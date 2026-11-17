"""Local-only synthetic RED extraction. This is NOT a trained/tuned model."""
from fastapi import Depends, FastAPI, HTTPException

from common.audit import audit
from common.catalog import TUNED_LABEL, extract, trusted
from common.config import get_settings
from common.health import health_router
from common.schemas import Candidate, InferRequest, SourceType, Zone
from common.security import require_internal

app = FastAPI(title="DaoBrain daobrain_tuned — untrained local simulation")
app.include_router(health_router("daobrain_tuned"))


@app.post("/reason", response_model=Candidate, dependencies=[Depends(require_internal)])
def reason(req: InferRequest) -> Candidate:
    if req.zone != Zone.RED:
        raise HTTPException(403, "模拟推理仅处理红区合成样例")
    if not req.evidence:
        raise HTTPException(422, "缺少红区合成证据")
    if any(e.zone != Zone.RED or not trusted(e) or e.provenance.source_type != SourceType.ORIGINAL
           or not e.provenance.source_id.startswith("synthetic-red-") for e in req.evidence):
        raise HTTPException(403, "仅接受受信红区合成样例")
    if len(req.task_id) > 128 or len(req.query.encode()) + sum(len(e.text.encode()) for e in req.evidence) > 8000:
        raise HTTPException(413, "模拟输入超限")
    try:
        answer, selected = extract(req.evidence, TUNED_LABEL,
                                   min(4096, get_settings().model_max_output_tokens * 8))
    except ValueError:
        raise HTTPException(413, "完整摘录超出输出上限") from None
    audit("daobrain_tuned", "synthetic_extraction", task_id=req.task_id,
          zone=Zone.RED.value, simulated=True, evidence_count=len(selected))
    return Candidate(task_id=req.task_id, answer=answer, evidence=selected,
                     model="daobrain-untrained-simulation", from_zone=Zone.RED)
