"""Protected local-only dispatch with bounded simulation accounting and no fallback.

GREEN -> rag_green -> local_model -> output_verify.
RED -> rag_red -> untrained synthetic reasoner -> gate (release remains denied).
Lexical retrieval and rule verification remain simulated even with real Ollama.
"""
from __future__ import annotations

from decimal import Decimal
import json

import httpx
from fastapi import Depends, FastAPI, HTTPException

from common.audit import audit
from common.catalog import public_query, search, supported_quotes, trusted
from common.config import get_settings
from common.health import health_router
from common.limits import BudgetExceeded, TaskBudget
from common.schemas import Answer, Candidate, Retrieved, TaskEnvelope, VerificationResult, VerifyStatus, Zone
from common.security import internal_headers, require_internal

app = FastAPI(title="DaoBrain hybrid_iface — local simulation pipeline")
app.include_router(health_router("hybrid_iface"))


def _post(client: httpx.Client, url: str, payload: dict):
    """Bound decoded response bytes and never follow redirects or ambient proxies."""
    with client.stream("POST", url, json=payload, headers=internal_headers()) as response:
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError("unexpected redirect")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 131072:
                raise ValueError("oversized service response")
        return json.loads(body)


def _refused(env: TaskEnvelope, trace: list[str], reason: str) -> Answer:
    audit("hybrid_iface", "refused", task_id=env.task_id, zone=env.zone.value, reason=reason)
    return Answer(task_id=env.task_id, answer="", evidence=[], refused=True, model="none",
                  note=reason, trace=trace, simulated=True,
                  verification=VerificationResult(task_id=env.task_id, status=VerifyStatus.REJECT,
                                                  provenance_ok=False, compliance_ok=False, reasons=[reason]))


def _valid_candidate(cand: Candidate, env: TaskEnvelope, retrieved: list[Retrieved]) -> bool:
    return (cand.task_id == env.task_id and cand.from_zone == env.zone and bool(cand.evidence)
            and all(e.zone == env.zone and trusted(e) and e in retrieved for e in cand.evidence)
            and supported_quotes(cand.answer, cand.evidence))


@app.post("/dispatch", response_model=Answer, dependencies=[Depends(require_internal)])
def dispatch(env: TaskEnvelope) -> Answer:
    trace = ["hybrid_iface"]
    if env.zone not in (Zone.GREEN, Zone.RED):
        return _refused(env, trace, "未分类请求失败关闭")
    if env.zone == Zone.GREEN and not public_query(env.query):
        return _refused(env, trace, "不是已知公开主题；禁止按客户端绿区标签推断安全")
    settings = get_settings()
    budget = TaskBudget(task_id=env.task_id, iteration_limit=env.max_iterations,
                        cost_limit=env.budget_cny)
    try:
        budget.consume_text(env.query)
        budget.next_iteration()
        # Conservative simulation reservation, NOT an invoice or measured model cost.
        budget.add(cost_cny=Decimal("0.01"))
        with httpx.Client(timeout=httpx.Timeout(settings.model_timeout_seconds),
                          trust_env=False, follow_redirects=False) as client:
            rag = "rag_green" if env.zone == Zone.GREEN else "rag_red"
            rag_url = settings.rag_green_url if env.zone == Zone.GREEN else settings.rag_red_url
            trace.append(rag)
            raw = _post(client, rag_url.rstrip("/") + "/retrieve",
                        {"query": env.query, "top_k": env.top_k})
            if not isinstance(raw, list) or len(raw) > env.top_k:
                raise ValueError("invalid retrieval response")
            evidence = [Retrieved.model_validate(item) for item in raw]
            allowed = search(env.query, env.top_k, env.zone)
            if any(e.zone != env.zone or not trusted(e) or e not in allowed for e in evidence):
                return _refused(env, trace, "检索证据不受信或区标签不匹配")
            if not evidence:
                return _refused(env, trace, "本区无匹配证据；不跨区回退")
            for e in evidence:
                budget.consume_text(e.text)
            model_module = "local_model" if env.zone == Zone.GREEN else "daobrain_tuned"
            model_url = settings.local_model_url if env.zone == Zone.GREEN else settings.daobrain_tuned_url
            endpoint = "/infer" if env.zone == Zone.GREEN else "/reason"
            trace.append(model_module)
            cand = Candidate.model_validate(_post(client, model_url.rstrip("/") + endpoint,
                {"task_id": env.task_id, "query": env.query, "zone": env.zone.value,
                 "evidence": [e.model_dump(mode="json") for e in evidence]}))
            if not _valid_candidate(cand, env, evidence):
                return _refused(env, trace, "本地候选内容或证据无法核验")
            if env.zone == Zone.GREEN:
                expected_model = ("local-extract-simulation" if settings.local_model_backend == "stub"
                                  else "ollama:" + settings.local_model_name)
            else:
                expected_model = "daobrain-untrained-simulation"
            if cand.model != expected_model:
                return _refused(env, trace, "本地候选模型标识不匹配")
            budget.consume_text(cand.answer)
            if env.zone == Zone.RED:
                trace.append("redline_gate")
                try:
                    gated = Candidate.model_validate(_post(client,
                        settings.redline_gate_url.rstrip("/") + "/gate", cand.model_dump(mode="json")))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 403:
                        return _refused(env, trace, "红区发布被门禁拒绝，须独立人工复核；未输出红区内容")
                    raise
                # No gate response can enable a release path that is not implemented.
                if gated != cand or gated.from_zone != Zone.RED:
                    return _refused(env, trace, "门禁尝试改变红区候选，拒绝发布")
                return _refused(env, trace, "红转绿独立人工复核流程未实现，拒绝发布")
            trace.append("output_verify")
            result = VerificationResult.model_validate(_post(client,
                settings.output_verify_url.rstrip("/") + "/verify", cand.model_dump(mode="json")))
            if (result.task_id != env.task_id or result.status != VerifyStatus.PASS
                    or not result.provenance_ok or not result.compliance_ok):
                return _refused(env, trace, "统一输出验证未通过；未输出候选内容")
            audit("hybrid_iface", "passed", task_id=env.task_id, zone=env.zone.value,
                  trace=trace, simulated=True, reserved_cny=str(budget.cost_cny),
                  byte_upper_bound=budget.tokens)
            return Answer(task_id=env.task_id, answer=cand.answer,
                          evidence=[e.provenance for e in cand.evidence], verification=result,
                          refused=False, model=cand.model, trace=trace, simulated=True,
                          note="本地词法检索与规则验证模拟；预算为保守模拟预留，非实测费用。")
    except BudgetExceeded:
        return _refused(env, trace, "任务输入、输出、迭代或模拟预算超限")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 413, 422):
            return _refused(env, trace, "下游本地服务拒绝请求；未输出内容")
        audit("hybrid_iface", "service_unavailable", task_id=env.task_id, zone=env.zone.value, trace=trace)
        raise HTTPException(503, "本地服务不可用；未启用公开或外部回退") from None
    except (httpx.HTTPError, ValueError, TypeError, UnicodeError):
        audit("hybrid_iface", "service_unavailable", task_id=env.task_id, zone=env.zone.value, trace=trace)
        raise HTTPException(503, "本地服务失败或响应无效；未启用公开或外部回退") from None
