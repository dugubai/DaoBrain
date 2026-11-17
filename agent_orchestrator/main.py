"""② 公开模拟编排：检索→服务端批准→外部stub→规则验证；只释放PASS。"""
import httpx
from fastapi import Depends, FastAPI, HTTPException
from common.config import get_settings
from common.health import health_router
from common.schemas import TaskEnvelope, Candidate, Retrieved, Answer, VerificationResult, Zone, VerifyStatus
from common.security import require_internal, internal_headers, sign_approval
from common.catalog import public_query, trusted, search, extract, RETRIEVAL_LABEL, supported_quotes
from common.audit import audit
from common.limits import TaskBudget, BudgetExceeded

app = FastAPI(title="DaoBrain ② agent_orchestrator")
app.include_router(health_router("agent_orchestrator"))


def refused(env: TaskEnvelope, reason: str, trace: list[str]) -> Answer:
    return Answer(task_id=env.task_id, answer="", refused=True, note=reason, trace=trace,
                  verification=VerificationResult(task_id=env.task_id, status=VerifyStatus.REJECT,
                                                  reasons=[reason]))


@app.post("/plan", response_model=Answer, dependencies=[Depends(require_internal)])
def plan(env: TaskEnvelope):
    s = get_settings()
    trace = ["agent_orchestrator"]
    if env.zone != Zone.GREEN or not public_query(env.query):
        # 不向公开检索或外部适配发送红区/未知内容。
        return refused(env, "非可信公开样本问题，不进入公开链路", trace)
    budget = TaskBudget(env.task_id, iteration_limit=env.max_iterations, cost_limit=env.budget_cny)
    try:
        budget.consume_text(env.query)
        with httpx.Client(timeout=20, trust_env=False, headers=internal_headers()) as client:
            for round_index in range(min(env.max_iterations, s.max_iterations_per_task)):
                budget.next_iteration()
                response = client.post(s.rag_public_url + "/retrieve",
                                       json={"query": env.query, "top_k": min(env.top_k + round_index * 2, 20)})
                response.raise_for_status()
                evidence = [Retrieved.model_validate(e) for e in response.json()]
                trace.append("rag_public")
                allowed = search(env.query, min(env.top_k + round_index * 2, 20), Zone.GREEN)
                if (not evidence or len(evidence) > min(env.top_k + round_index * 2, 20)
                        or len({e.provenance.source_id for e in evidence}) != len(evidence)
                        or not all(e.zone == Zone.GREEN and trusted(e) and e in allowed for e in evidence)):
                    return refused(env, "无相关可信公开检索命中，已拒答", trace)
                budget.consume_text(" ".join(e.text for e in evidence))
                if s.allow_external_stub:
                    proof = sign_approval(env.task_id, env.query, evidence)
                    response = client.post(s.external_model_url + "/generate",
                                           json={"task_id": env.task_id, "query": env.query, "zone": "green",
                                                 "approval_proof": proof,
                                                 "evidence": [e.model_dump(mode="json") for e in evidence]})
                    response.raise_for_status()
                    generated = response.json()
                    text, model = generated["answer"], generated["model"]
                    trace.append("external_model")
                else:
                    text, evidence = extract(evidence, RETRIEVAL_LABEL)
                    model = "retrieval-compose"
                if not supported_quotes(text, evidence):
                    return refused(env, "候选包含不受证据支撑的内容，未交付", trace)
                budget.consume_text(text)
                candidate = Candidate(task_id=env.task_id, answer=text, evidence=evidence,
                                      from_zone=Zone.GREEN, model=model)
                response = client.post(s.output_verify_url + "/verify", json=candidate.model_dump(mode="json"))
                response.raise_for_status()
                result = VerificationResult.model_validate(response.json())
                trace.append("output_verify")
                if result.task_id != env.task_id:
                    return refused(env, "验证任务标识不匹配", trace)
                if result.status == VerifyStatus.PASS and result.provenance_ok and result.compliance_ok:
                    audit("agent_orchestrator", "done", task_id=env.task_id, status="pass", external=False)
                    return Answer(task_id=env.task_id, answer=text,
                                  evidence=[e.provenance for e in evidence], verification=result,
                                  model=model, trace=trace, note="模拟样本和规则验证；非临床结论。")
                if result.status == VerifyStatus.REJECT:
                    break
            return Answer(task_id=env.task_id, answer="", evidence=[], refused=True,
                          verification=result, trace=trace, note="验证未通过，拒答或转人工。")
    except BudgetExceeded:
        return refused(env, "任务额度或迭代上限，拒答", trace)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        audit("agent_orchestrator", "dependency_error", task_id=env.task_id, external=False)
        raise HTTPException(503, "依赖服务不可用或响应不合法；未释放候选答案")
