"""① 模拟入口：鉴权、单进程限速、公开/本地分派、最终失败关闭，以及流量埋点/问答落库。"""
import json
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from threading import Lock

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from common import energy_lens
from common import observe
from common.audit import audit
from common.catalog import public_query, search, supported_quotes
from common.config import get_settings
from common.health import health_router
from common.schemas import Answer, TaskEnvelope, VerificationResult, VerifyStatus, Zone
from common.security import internal_headers, require_api

app = FastAPI(title="DaoBrain ① gateway")
app.include_router(health_router("gateway"))
_recent = deque()
_lock = Lock()


@app.get("/", include_in_schema=False)
def portal():
    return FileResponse(Path(__file__).resolve().parents[1] / "portal" / "index.html")


def _tier(env):
    return env.zone.value if env.zone in (Zone.GREEN, Zone.RED) else "public"


def _emit(start, env, *, channel="", route_target="", verify="", released=False,
          outcome="", model="none", evidence_ids=(), answer=None):
    observe.record(task_hash=env.task_id, persona=env.user_id, query=env.query,
                   zone=env.zone.value, channel=channel, route_target=route_target,
                   tier=_tier(env), verify=verify, released=released, outcome=outcome,
                   model=model, evidence_count=len(evidence_ids),
                   evidence_ids=",".join(evidence_ids),
                   latency_ms=int((time.perf_counter() - start) * 1000), answer=answer)


def _energy_interpretation(query: str, result, s) -> str:
    """绿区命中后对已核验典籍原文, 另生成一段独立的能量模型哲学解读(作者框架,未核验,非诊疗)。
    尽力而为: 依赖失败/输出为空/触碰诊疗红线一律返回空串, 绝不阻断或改写已通过核验的答案。"""
    try:
        model = s.open_reason_model or s.local_model_name
        if not model:
            return ""
        parts = []
        for prov in result.evidence:
            label = (getattr(prov, "title", "") or "") + "：" + (getattr(prov, "quote", "") or "")
            if label not in parts:
                parts.append(label)
        ev = "；".join(parts)[:600]
        prompt = energy_lens.interpretation_prompt(query, ev)
        payload = {"model": model, "prompt": prompt, "stream": False, "keep_alive": "20s",
                   "options": {"num_predict": 160, "num_ctx": 2048, "num_batch": 8, "num_gpu": 0,
                               "use_mmap": True, "num_thread": s.model_cpu_threads,
                               "temperature": 0.5}}
        with httpx.Client(timeout=httpx.Timeout(90.0, connect=10.0), trust_env=False,
                          follow_redirects=False) as client:
            with client.stream("POST", s.ollama_url.rstrip("/") + "/api/generate", json=payload) as r:
                r.raise_for_status()
                if r.is_redirect:
                    raise ValueError("redirect")
                body = bytearray()
                for chunk in r.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 32768:
                        raise ValueError("oversized")
                data = json.loads(body)
        text = data.get("response") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip() or data.get("done") is not True:
            return ""
        if _MEDICAL.search(text):
            audit("gateway", "energy_interpretation_redline", task_id=result.task_id, external=False)
            return ""
        return text.strip()
    except Exception:
        audit("gateway", "energy_interpretation_error", task_id=getattr(result, "task_id", None), external=False)
        return ""


@app.post("/v1/query", response_model=Answer)
def query(env: TaskEnvelope, identity: str = Depends(require_api)):
    s = get_settings()
    start = time.perf_counter()
    now = time.monotonic()
    with _lock:
        while _recent and _recent[0] < now - 60:
            _recent.popleft()
        if len(_recent) >= s.max_requests_per_minute:
            raise HTTPException(429, "模拟入口请求过频，请稍后重试")
        _recent.append(now)
    env = env.model_copy(update={"task_id": env.task_id or uuid.uuid4().hex,
                                 "user_id": identity, "gate_approved": False})
    # 声称green/批准不构成公开授权；只接受明确可信的公开样本话题。
    if env.zone == Zone.UNKNOWN or (env.zone == Zone.GREEN and not public_query(env.query)):
        _emit(start, env, verify="reject", released=False, outcome="refuse_entry")
        return Answer(task_id=env.task_id, answer="", refused=True, trace=["gateway"],
                      note="未分类或不在可信公开样本范围，未派发公开链路。",
                      verification=VerificationResult(task_id=env.task_id, status=VerifyStatus.REJECT))
    audit("gateway", "recv", task_id=env.task_id, zone=env.zone.value, external=False)
    if env.zone == Zone.RED:
        target = s.hybrid_iface_url + "/dispatch"
    elif s.gateway_green_backend == "external_stub":
        target = s.orchestrator_url + "/plan"
    elif s.gateway_green_backend == "local_ollama":
        target = s.hybrid_iface_url + "/dispatch"
    else:
        raise HTTPException(503, "未知绿区后端；未启用回退")
    channel = "public_cloud" if "orchestrator" in target else "local"
    try:
        with httpx.Client(timeout=120, trust_env=False, follow_redirects=False,
                          headers=internal_headers()) as client:
            response = client.post(target, json=env.model_dump(mode="json"))
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("unexpected redirect")
            result = Answer.model_validate(response.json())
        allowed = search(env.query, 20, Zone.GREEN) if env.zone == Zone.GREEN else []
        by_source = {e.provenance.source_id: e for e in allowed}
        final_evidence = [by_source[p.source_id] for p in result.evidence
                          if p.source_id in by_source and p == by_source[p.source_id].provenance]
        if (env.zone != Zone.GREEN or result.task_id != env.task_id
                or result.verification.task_id != env.task_id
                or result.verification.status != VerifyStatus.PASS or result.refused
                or not result.verification.provenance_ok or not result.verification.compliance_ok
                or not result.evidence or len(final_evidence) != len(result.evidence)
                or not supported_quotes(result.answer, final_evidence)):
            _emit(start, env, channel=channel, route_target=target, verify="reject",
                  released=False, outcome="refuse_verify", model="none")
            return Answer(task_id=env.task_id, answer="", evidence=[], refused=True,
                          trace=["gateway"] + result.trace, model="none",
                          note="验证未通过、来源不可信或任务不匹配，未释放答案。",
                          verification=VerificationResult(task_id=env.task_id, status=VerifyStatus.REJECT,
                                                          reasons=["入口最终校验拒绝释放"]))
        _emit(start, env, channel=channel, route_target=target, verify="pass", released=True,
              outcome="release", model=result.model,
              evidence_ids=[e.provenance.source_id for e in final_evidence],
              answer=(result.answer if env.zone == Zone.GREEN else None))
        interp = ""
        if os.environ.get("ENERGY_LENS_ENABLED", "1") != "0" and env.zone == Zone.GREEN:
            interp = _energy_interpretation(env.query, result, s)
        return result.model_copy(update={"trace": ["gateway"] + result.trace,
                                         "interpretation": interp})
    except (httpx.HTTPError, ValueError, KeyError):
        audit("gateway", "dependency_error", task_id=env.task_id, external=False)
        _emit(start, env, channel=channel, route_target=target, verify="error",
              released=False, outcome="dependency_error")
        raise HTTPException(503, "依赖服务不可用；未释放未经验证的答案")



_MEDICAL = re.compile(
    r"\d+\s*(?:毫克|克|mg|ml|毫升)|(?:建议|应当|必须|可以|需要)(?:服用|用药|停药|吃药|服)|"
    r"确诊|诊断|治愈|根治|处方|用药|吃什么药|怎么治|如何治|怎样治|治疗方案|什么病|得了.*病|"
    r"症状|剂量|副作用|禁忌",
    re.I)
def _reason_disclaimer(model_name: str) -> str:
    return (f"（以下为 {model_name} 本地模型自由推理，未经典籍语料核验，仅作常识参考，不替代执业医师诊疗。）")


@app.post("/v1/reason", response_model=Answer)
def reason(env: TaskEnvelope, identity: str = Depends(require_api)):
    """①-b 开放推理层：语料外的常识问题交由本地小模型自由作答，明确标注未经核验；诊疗/用药类硬拒。"""
    s = get_settings()
    start = time.perf_counter()
    tid = env.task_id or uuid.uuid4().hex
    trace = ["gateway", "open_reason"]
    env = env.model_copy(update={"task_id": tid, "user_id": identity})

    def _redline_refuse(note: str, reason: str) -> Answer:
        _emit(start, env, channel="local", route_target=s.ollama_url, verify="reject",
              released=False, outcome="refuse_redline", model="none")
        return Answer(task_id=tid, answer="", evidence=[], refused=True, trace=trace, model="none",
                      note=note,
                      verification=VerificationResult(task_id=tid, status=VerifyStatus.REJECT,
                          provenance_ok=False, compliance_ok=False, reasons=[reason]))

    if not s.open_reason_enabled:
        raise HTTPException(503, "开放推理层未启用")
    q = (env.query or "").strip()
    if _MEDICAL.search(q):
        return _redline_refuse("涉及诊断或用药等诊疗内容，模型不予推理作答。", "诊疗类请求，推理层据实拒答")
    if observe.sensitive_hit(q):
        return _redline_refuse("涉及隐私、凭证或外链等敏感信息，推理层不予作答。", "敏感信息请求，推理层据实拒答")

    model_name = s.open_reason_model or s.local_model_name
    if not model_name or len(model_name) > 128:
        raise HTTPException(503, "开放推理模型配置无效")
    prompt = (energy_lens.reason_framing() + " 请以上述天干能量模型的文化视角简要作答，不得给出诊断、用药或剂量；"
              "不确定就说不确定。用中文，80字以内。\n问：" + q[:500])
    payload = {"model": model_name, "prompt": prompt, "stream": False, "keep_alive": "20s",
               "options": {"num_predict": 220, "num_ctx": 4096, "num_batch": 8, "num_gpu": 0,
                           "use_mmap": True, "num_thread": s.model_cpu_threads, "temperature": 0.4}}
    try:
        with httpx.Client(timeout=httpx.Timeout(s.model_timeout_seconds), trust_env=False,
                          follow_redirects=False) as client:
            with client.stream("POST", s.ollama_url.rstrip("/") + "/api/generate", json=payload) as r:
                r.raise_for_status()
                if r.is_redirect:
                    raise ValueError("redirect")
                body = bytearray()
                for ch in r.iter_bytes():
                    body.extend(ch)
                    if len(body) > 32768:
                        raise ValueError("oversized model response")
                data = json.loads(body)
        text = data.get("response") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip() or data.get("done") is not True:
            raise ValueError("bad model output")
        if _MEDICAL.search(text):
            return _redline_refuse("模型推理触及诊疗表述，已据实拦下，未交付。", "推理答案含诊疗/剂量表述")
        if observe.sensitive_hit(text):
            return _redline_refuse("模型推理输出触及隐私/凭证/外链等敏感表述，已据实拦下，未交付。", "推理答案含敏感/隐私信息")
        answer_text = _reason_disclaimer(model_name) + "\n" + text.strip()
        _emit(start, env, channel="local", route_target=s.ollama_url, verify="revise",
              released=True, outcome="reason", model="ollama:" + model_name, answer=answer_text)
        return Answer(task_id=tid, answer=answer_text, evidence=[],
                      verification=VerificationResult(task_id=tid, status=VerifyStatus.REVISE,
                          provenance_ok=False, compliance_ok=True, hallucination_risk=1.0,
                          reasons=["开放推理，未经典籍验证"]),
                      refused=False, model="ollama:" + model_name, trace=trace, simulated=False,
                      note="模型推理·未经典籍验证·非诊疗建议")
    except (httpx.HTTPError, ValueError, TypeError, KeyError, UnicodeError):
        audit("gateway", "reason_dependency_error", task_id=tid, external=False)
        _emit(start, env, channel="local", route_target=s.ollama_url, verify="error",
              released=False, outcome="dependency_error")
        raise HTTPException(503, "依赖服务不可用；未释放未经验证的答案")


@app.get("/v1/metrics")
def get_metrics(minutes: int = Query(default=60, ge=0, le=1440), _identity: str = Depends(require_api)):
    try:
        return observe.metrics(minutes or 0)
    except Exception:
        raise HTTPException(503, "指标存储暂不可用")


@app.get("/v1/qa/list")
def get_qa_list(limit: int = Query(default=200, ge=1, le=1000), _identity: str = Depends(require_api)):
    try:
        return {"qa": observe.qa_list(limit)}
    except Exception:
        raise HTTPException(503, "问答存储暂不可用")


@app.get("/v1/qa/detail")
def get_qa_detail(id: int = Query(..., ge=1), _identity: str = Depends(require_api)):
    try:
        detail = observe.qa_detail(id)
    except Exception:
        raise HTTPException(503, "问答存储暂不可用")
    if detail is None:
        raise HTTPException(404, "记录不存在")
    return detail
