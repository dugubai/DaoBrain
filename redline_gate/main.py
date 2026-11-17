"""Auditable fail-closed gate. RED release awaits independent manual review.

There is no implemented red-to-green authorization, including client approval flags.
The existing GREEN path checks exact canonical evidence and supported quotations.
"""
import hashlib

from fastapi import Depends, FastAPI, HTTPException

from common.audit import audit
from common.catalog import supported_quotes, trusted
from common.health import health_router
from common.schemas import Candidate, Zone
from common.security import require_internal

app = FastAPI(title="DaoBrain redline_gate — fail closed")
app.include_router(health_router("redline_gate"))


@app.post("/gate", response_model=Candidate, dependencies=[Depends(require_internal)])
def gate(cand: Candidate) -> Candidate:
    # Only opaque metadata, never query/answer/evidence text, enters audit logs.
    task_ref = hashlib.sha256(cand.task_id.encode()).hexdigest()[:16]
    if cand.from_zone != Zone.GREEN:
        audit("redline_gate", "denied", task_id=task_ref, zone=cand.from_zone.value,
              status="independent_manual_review_required", count=len(cand.evidence))
        raise HTTPException(403, "红区及未分类候选禁止发布；红转绿须独立人工复核，客户端批准无效")
    if (len(cand.task_id) > 128 or not cand.evidence
            or any(e.zone != Zone.GREEN or not trusted(e) for e in cand.evidence)
            or not supported_quotes(cand.answer, cand.evidence)):
        audit("redline_gate", "denied", task_id=task_ref, zone=cand.from_zone.value,
              status="untrusted_or_unsupported_green_candidate")
        raise HTTPException(403, "绿区候选证据或引文无法核验")
    audit("redline_gate", "passed_existing_green", task_id=task_ref, zone=Zone.GREEN.value,
          count=len(cand.evidence))
    return cand
