"""① 模拟入口：鉴权、单进程限速、公开/本地分派与最终失败关闭。"""
from collections import deque
from pathlib import Path
from threading import Lock
import time
import uuid
import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from common.config import get_settings
from common.health import health_router
from common.schemas import TaskEnvelope, Answer, Zone, VerifyStatus, VerificationResult
from common.security import require_api, internal_headers
from common.catalog import public_query, search, supported_quotes
from common.audit import audit

app = FastAPI(title="DaoBrain ① gateway")
app.include_router(health_router("gateway"))
_recent = deque()
_lock = Lock()


@app.get("/", include_in_schema=False)
def portal():
    return FileResponse(Path(__file__).resolve().parents[1] / "portal" / "index.html")


@app.post("/v1/query", response_model=Answer)
def query(env: TaskEnvelope, identity: str = Depends(require_api)):
    s = get_settings()
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
            return Answer(task_id=env.task_id, answer="", evidence=[], refused=True,
                          trace=["gateway"] + result.trace, model="none",
                          note="验证未通过、来源不可信或任务不匹配，未释放答案。",
                          verification=VerificationResult(task_id=env.task_id, status=VerifyStatus.REJECT,
                                                          reasons=["入口最终校验拒绝释放"]))
        return result.model_copy(update={"trace": ["gateway"] + result.trace})
    except (httpx.HTTPError, ValueError, KeyError):
        audit("gateway", "dependency_error", task_id=env.task_id, external=False)
        raise HTTPException(503, "依赖服务不可用；未释放未经验证的答案")
