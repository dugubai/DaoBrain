"""⑩ 模拟输出验证：可信样本出处、字面证据和合规规则。不是临床或全面事实核验。"""
import re
from fastapi import Depends, FastAPI
from common.health import health_router
from common.schemas import Candidate, VerificationResult, VerifyStatus, Zone
from common.security import require_internal
from common.catalog import trusted, supported_quotes

app = FastAPI(title="DaoBrain ⑩ output_verify")
app.include_router(health_router("output_verify"))


@app.post("/verify", response_model=VerificationResult, dependencies=[Depends(require_internal)])
def verify(cand: Candidate):
    reasons = []
    provenance_ok = bool(cand.evidence) and all(trusted(e) for e in cand.evidence)
    green = cand.from_zone == Zone.GREEN and all(e.zone == Zone.GREEN for e in cand.evidence)
    supported = supported_quotes(cand.answer, cand.evidence)
    medical_risk = bool(re.search(r"\d+\s*(?:毫克|克|mg|ml|毫升)|(?:建议|应当|必须|可以)(?:服用|用药|停药)|确诊|保证治愈", cand.answer, re.I))
    compliance_ok = (not medical_risk and "不替代执业医师" in cand.answer)
    if not green:
        reasons.append("非绿区候选或混入红区/未分类证据")
    if not provenance_ok:
        reasons.append("出处不在可信样本库或版本/原文/hash被改动")
    if not supported:
        reasons.append("无可验证的字面证据支撑")
    if medical_risk:
        reasons.append("存在诊断或剂量等不适宜文化辅助输出")
    if not compliance_ok:
        reasons.append("合规提示缺失或医疗表述超出范围")
    if not green or not provenance_ok or not supported or medical_risk:
        status = VerifyStatus.REJECT
    elif not compliance_ok:
        status = VerifyStatus.REVISE
    else:
        status = VerifyStatus.PASS
    return VerificationResult(task_id=cand.task_id, status=status,
                              hallucination_risk=0.0 if status == VerifyStatus.PASS else 1.0,
                              provenance_ok=provenance_ok, compliance_ok=compliance_ok,
                              reasons=reasons)
