"""⑤ 外部适配模拟。实际外部API未接入，live模式明确失败而不伪造结果。"""
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from common.config import get_settings
from common.health import health_router
from common.schemas import Retrieved, Zone
from common.security import require_internal, check_approval
from common.audit import audit
from common.catalog import trusted, public_query, search, extract, EXTERNAL_LABEL

app = FastAPI(title="DaoBrain ⑤ external_model")
app.include_router(health_router("external_model"))


class GenReq(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=2000)
    zone: Zone = Zone.UNKNOWN
    gate_approved: bool = False  # 不作为批准凭证。
    approval_proof: str = ""
    evidence: list[Retrieved] = Field(default_factory=list, max_length=20)


@app.post("/generate", dependencies=[Depends(require_internal)])
def generate(req: GenReq):
    s = get_settings()
    safe = (req.zone == Zone.GREEN and public_query(req.query) and bool(req.evidence)
            and all(e.zone == Zone.GREEN and trusted(e) and e in search(req.query, 20, Zone.GREEN) for e in req.evidence)
            and check_approval(req.approval_proof, req.task_id, req.query, req.evidence))
    if not safe:
        audit("external_model", "blocked", task_id=req.task_id, external=False)
        raise HTTPException(403, "缺少服务端批准或公开可信证据，拒绝外部适配")
    if s.external_model_mode != "stub":
        audit("external_model", "live_unavailable", task_id=req.task_id, external=False)
        raise HTTPException(503, "真实外部API未接入；未发生出网调用")
    if not s.allow_external_stub:
        raise HTTPException(403, "服务端禁用外部模拟")
    # 所有声明的证据必须完整摘录，不允许重复或未使用的出处。
    if len({e.provenance.source_id for e in req.evidence}) != len(req.evidence):
        raise HTTPException(403, "重复证据，拒绝模拟生成")
    answer, selected = extract(req.evidence, EXTERNAL_LABEL)
    if selected != req.evidence:
        raise HTTPException(413, "证据不能在输出限额内完整摘录")
    audit("external_model", "stub_call", task_id=req.task_id, external=False)
    return {"answer": answer, "model": "external-stub", "simulated": True}
