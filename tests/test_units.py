"""核心回归：可信证据、鉴权、批准绑定、预算和失败关闭；无真实外部调用。"""
from decimal import Decimal
import httpx
import pytest
from fastapi.testclient import TestClient
from common.catalog import catalog, search, extract, EXTERNAL_LABEL
from common.config import get_settings
from common.schemas import Candidate, TaskEnvelope, Zone
from common.security import sign_approval, check_approval
from common.limits import TaskBudget, BudgetExceeded
from external_model.main import app as ext_app
from output_verify.main import app as verify_app
from agent_orchestrator.main import app as orch_app
from gateway.main import app as gateway_app


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-user-key")
    monkeypatch.setenv("INTERNAL_API_KEY", "test-service-key")
    monkeypatch.setenv("APPROVAL_SECRET", "test-approval-key")
    monkeypatch.setenv("EXTERNAL_MODEL_MODE", "stub")
    monkeypatch.setenv("ALLOW_EXTERNAL_STUB", "true")
    get_settings.cache_clear()
    from gateway.main import _recent
    _recent.clear()
    yield
    get_settings.cache_clear()
    _recent.clear()


I = {"X-Internal-Key": "test-service-key"}
A = {"X-API-Key": "test-user-key"}


def evidence():
    return search("四时养生 法于阴阳", 4, Zone.GREEN)


def generation():
    ev = evidence()
    query = "四时养生 法于阴阳"
    return {"task_id": "test-gen", "query": query, "zone": "green", "gate_approved": True,
            "approval_proof": sign_approval("test-gen", query, ev),
            "evidence": [e.model_dump(mode="json") for e in ev]}


def candidate():
    text, ev = extract(evidence(), EXTERNAL_LABEL)
    return Candidate(task_id="verify-test", answer=text,
                     evidence=ev, from_zone=Zone.GREEN, model="external-stub")


def test_gateway_auth_before_dispatch(monkeypatch):
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **kw: pytest.fail("未鉴权不得派发"))
    with TestClient(gateway_app) as client:
        assert client.request("POST", "/v1/query", json={"query": "四时养生", "zone": "green"}).status_code == 401


def test_missing_keys_fail_closed(monkeypatch):
    monkeypatch.setenv("API_KEY", "")
    get_settings.cache_clear()
    assert TestClient(gateway_app).post("/v1/query", headers=A, json={"query": "四时养生"}).status_code == 503


def test_generation_requires_service_auth():
    assert TestClient(ext_app).post("/generate", json=generation()).status_code == 401


def test_client_gate_flag_not_proof():
    payload = generation()
    payload["approval_proof"] = ""
    assert TestClient(ext_app).post("/generate", headers=I, json=payload).status_code == 403


@pytest.mark.parametrize("change", ["query", "task", "evidence", "red", "unknown"])
def test_approval_binds_entire_payload(change):
    payload = generation()
    if change == "query":
        payload["query"] = "素问 上古天真论"
    elif change == "task":
        payload["task_id"] = "other-task"
    elif change == "evidence":
        payload["evidence"][0]["text"] = "恶意替换原文"
    else:
        payload["zone"] = change
    assert TestClient(ext_app).post("/generate", headers=I, json=payload).status_code == 403


def test_expired_approval_rejects(monkeypatch):
    from common import security
    original = security.time.time
    proof = sign_approval("t", "四时养生", evidence())
    now = original()
    monkeypatch.setattr(security.time, "time", lambda: now + 120)
    assert not check_approval(proof, "t", "四时养生", evidence())


def test_external_stub_truthful():
    response = TestClient(ext_app).post("/generate", headers=I, json=generation())
    assert response.status_code == 200
    assert response.json()["model"] == "external-stub"
    assert response.json()["simulated"] is True
    assert evidence()[0].text in response.json()["answer"]


def test_live_is_not_faked(monkeypatch):
    monkeypatch.setenv("EXTERNAL_MODEL_MODE", "live")
    get_settings.cache_clear()
    assert TestClient(ext_app).post("/generate", headers=I, json=generation()).status_code == 503


def test_verify_known_exact_evidence_pass():
    response = TestClient(verify_app).post("/verify", headers=I, json=candidate().model_dump(mode="json"))
    assert response.json()["status"] == "pass"


@pytest.mark.parametrize("change", ["hash", "version", "text", "zone", "missing", "noquote", "dose"])
def test_verifier_rejects_tamper_unsupported_or_medical(change):
    payload = candidate().model_dump(mode="json")
    if change == "hash":
        payload["evidence"][0]["provenance"]["content_hash"] = "0" * 64
    elif change == "version":
        payload["evidence"][0]["provenance"]["version"] = "forged"
    elif change == "text":
        payload["evidence"][0]["text"] = "伪造原文"
    elif change == "zone":
        payload["evidence"][0]["zone"] = "red"
    elif change == "missing":
        payload["evidence"] = []
    elif change == "noquote":
        payload["answer"] = "没有出处支撑的天文断言。本内容不替代执业医师诊疗。"
    else:
        payload["answer"] += " 建议服用3克药物。"
    result = TestClient(verify_app).post("/verify", headers=I, json=payload).json()
    assert result["status"] == "reject", result


@pytest.mark.parametrize("zone", ["red", "unknown"])
def test_orchestrator_blocks_before_any_network(monkeypatch, zone):
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **kw: pytest.fail("非绿区不得联网"))
    response = TestClient(orch_app).request("POST", "/plan", headers=I,
                                       json={"task_id": "non-green", "query": "内部样本", "zone": zone})
    assert response.status_code == 200
    assert response.json()["refused"] is True
    assert response.json()["answer"] == ""


def fake_network(monkeypatch, verification="revise"):
    ev = evidence()
    calls = []
    def post(client, url, **kwargs):
        calls.append(url)
        if url.endswith("/retrieve"):
            data = [e.model_dump(mode="json") for e in ev]
        elif url.endswith("/generate"):
            data = {"answer": candidate().answer, "model": "external-stub"}
        elif url.endswith("/verify"):
            data = {"task_id": "flow", "status": verification, "provenance_ok": True,
                    "compliance_ok": verification == "pass", "reasons": []}
        else:
            pytest.fail(url)
        return httpx.Response(200, json=data, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", post)
    return calls


def test_revise_never_reaches_user_and_loop_is_bounded(monkeypatch):
    calls = fake_network(monkeypatch)
    result = TestClient(orch_app).request("POST", "/plan", headers=I,
                                     json={"task_id": "flow", "query": "四时养生 法于阴阳", "zone": "green", "max_iterations": 2}).json()
    assert result["answer"] == "" and result["evidence"] == [] and result["refused"] is True
    assert len([url for url in calls if url.endswith("/retrieve")]) <= 2


def test_public_success_requires_pass(monkeypatch):
    fake_network(monkeypatch, "pass")
    result = TestClient(orch_app).request("POST", "/plan", headers=I,
                                     json={"task_id": "flow", "query": "四时养生 法于阴阳", "zone": "green"}).json()
    assert result["verification"]["status"] == "pass" and not result["refused"]


def test_budget_decimal_and_atomic_check():
    budget = TaskBudget("money", cost_limit=Decimal("0.30"), token_limit=5, iteration_limit=1)
    budget.add(tokens=1, cost_cny=Decimal("0.10"))
    budget.add(cost_cny=Decimal("0.20"))
    assert budget.cost_cny == Decimal("0.30")
    with pytest.raises(BudgetExceeded):
        budget.add(cost_cny=Decimal("0.01"))
    assert budget.cost_cny == Decimal("0.30")
    budget.next_iteration()
    with pytest.raises(BudgetExceeded):
        budget.next_iteration()
    with pytest.raises(BudgetExceeded):
        budget.consume_text("中中")
    with pytest.raises(ValueError):
        budget.add(tokens=-1)


@pytest.mark.parametrize("data", [{"max_iterations": 0}, {"max_iterations": 7}, {"budget_cny": "NaN"}, {"budget_cny": "-0.1"}, {"top_k": 21}, {"query": ""}])
def test_envelope_input_bounds(data):
    with pytest.raises(ValueError):
        TaskEnvelope.model_validate({"query": "四时养生", **data})


def test_verifier_rejects_valid_quote_plus_arbitrary_claim():
    payload = candidate().model_dump(mode="json")
    payload["answer"] += "\n月球由奶酪构成。"
    response = TestClient(verify_app).post("/verify", headers=I, json=payload)
    assert response.json()["status"] == "reject"


def test_verifier_rejects_unused_evidence():
    payload = candidate().model_dump(mode="json")
    extra = next(e for e in catalog(Zone.GREEN) if e.provenance.source_id not in {p["provenance"]["source_id"] for p in payload["evidence"]})
    payload["evidence"].append(extra.model_dump(mode="json"))
    response = TestClient(verify_app).post("/verify", headers=I, json=payload)
    assert response.json()["status"] == "reject"


@pytest.mark.parametrize("change", ["empty-evidence", "bad-provenance", "unsupported", "wrong-task", "wrong-verify-task", "revise", "red-pass"])
def test_gateway_rejects_forged_downstream_success(monkeypatch, change):
    from common.schemas import Answer, VerificationResult, VerifyStatus
    cand = candidate()
    downstream = Answer(task_id="guard", answer=cand.answer, evidence=[e.provenance for e in cand.evidence],
                        model="external-stub", verification=VerificationResult(task_id="guard", status=VerifyStatus.PASS,
                                                                                provenance_ok=True, compliance_ok=True)).model_dump(mode="json")
    if change == "empty-evidence":
        downstream["evidence"] = []
    elif change == "bad-provenance":
        downstream["evidence"][0]["content_hash"] = "bad"
    elif change == "unsupported":
        downstream["answer"] += "\n月球由奶酪构成。"
    elif change == "wrong-task":
        downstream["task_id"] = "other"
    elif change == "wrong-verify-task":
        downstream["verification"]["task_id"] = "other"
    elif change == "revise":
        downstream["verification"]["status"] = "revise"
    def post(client, url, **kwargs):
        return httpx.Response(200, json=downstream, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", post)
    response = TestClient(gateway_app).request("POST", "/v1/query", headers=A,
                       json={"task_id": "guard", "query": "虚构流程甲" if change == "red-pass" else "四时养生 法于阴阳",
                             "zone": "red" if change == "red-pass" else "green"})
    body = response.json()
    assert body["refused"] and body["answer"] == "" and body["evidence"] == []
    assert body["verification"]["status"] == "reject" and body["verification"]["task_id"] == "guard"
    assert body["model"] == "none" and body["verification"]["reasons"] == ["入口最终校验拒绝释放"]
