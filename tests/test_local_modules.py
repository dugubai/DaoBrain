"""Unit contracts and in-process integrated local flow; all HTTP/Ollama calls mocked."""
from __future__ import annotations

import hashlib
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from common.catalog import (DISCLAIMER, OLLAMA_LABEL, STUB_LABEL, TUNED_LABEL,
                            VERSION, catalog, extract, public_query, search,
                            supported_quotes, trusted)
from common.config import get_settings
from common.schemas import Candidate, Zone
from daobrain_tuned.main import app as tuned_app
from hybrid_iface.main import app as hybrid_app
from local_model.main import app as local_app
from output_verify.main import app as verify_app
from rag_green.main import app as green_app
from rag_public.main import app as public_app
from rag_red.main import app as red_app
from redline_gate.main import app as gate_app

_REAL_CLIENT = httpx.Client
HEADERS = {"X-Internal-Key": "local-module-test-key"}


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    # No module-level environment writes: every setting is restored by monkeypatch.
    for key, value in {
        "INTERNAL_API_KEY": "local-module-test-key", "API_KEY": "local-test-api",
        "LOCAL_MODEL_BACKEND": "stub", "LOCAL_MODEL_NAME": "test-local-model",
        "OLLAMA_URL": "http://ollama:11434", "MODEL_TIMEOUT_SECONDS": "5",
        # Mocked copy contracts must not inherit CPU deployment's select/1024 settings.
        "MODEL_OUTPUT_PROTOCOL": "copy", "MODEL_CONTEXT_TOKENS": "4096",
        "MODEL_CPU_THREADS": "2",
        "MODEL_MAX_OUTPUT_TOKENS": "256", "MAX_TOKENS_PER_TASK": "8000",
        "MAX_ITERATIONS_PER_TASK": "6", "MAX_COST_CNY_PER_TASK": "0.50",
        "RAG_GREEN_URL": "http://rag_green:8007", "RAG_RED_URL": "http://rag_red:8008",
        "LOCAL_MODEL_URL": "http://local_model:8006", "DAOBRAIN_TUNED_URL": "http://daobrain_tuned:8011",
        "REDLINE_GATE_URL": "http://redline_gate:8009", "OUTPUT_VERIFY_URL": "http://output_verify:8010",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def request_payload(zone=Zone.GREEN):
    # 请求夹具不是全库导出；保持最多四条及原有篡改、隔离测试意图。
    return {"task_id": "local-test", "query": "四时养生 法于阴阳" if zone == Zone.GREEN else "虚构流程甲",
            "zone": zone.value, "evidence": [e.model_dump(mode="json") for e in catalog(zone)[:4]]}


def candidate(zone=Zone.GREEN):
    ev = catalog(zone)[:2]
    answer, ev = extract(ev, STUB_LABEL if zone == Zone.GREEN else TUNED_LABEL)
    return Candidate(task_id="local-test", answer=answer, evidence=ev, from_zone=zone,
                     model="local-extract-simulation" if zone == Zone.GREEN else "daobrain-untrained-simulation")


def mock_http(monkeypatch, handler):
    def client_factory(*args, **kwargs):
        assert kwargs.get("follow_redirects") is False
        assert kwargs.get("trust_env") is False
        return _REAL_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "Client", client_factory)


@pytest.mark.parametrize("zone,size", [(Zone.GREEN, 22), (Zone.RED, 12), (Zone.UNKNOWN, 0)])
def test_catalog_zones_hashes_and_isolation(zone, size):
    items = catalog(zone)
    assert len(items) == size
    for item in items:
        assert trusted(item) and item.zone == zone
        assert item.provenance.quote in item.text
        expected = hashlib.sha256((item.text + "\n" + VERSION).encode()).hexdigest()
        assert item.provenance.content_hash == expected
    if items:
        items[0].provenance.title = "tampered"
        assert not trusted(items[0])
        assert trusted(catalog(zone)[0])
    if zone == Zone.RED:
        assert all("纯合成演示" in e.text and "无真实用户秘密" in e.text for e in items)


@pytest.mark.parametrize("field,value", [
    ("text", "tampered"), ("zone", Zone.UNKNOWN), ("provenance.source_id", "bad"),
    ("provenance.title", "bad"), ("provenance.locator", "bad"),
    ("provenance.source_type", "original"), ("provenance.quote", "bad"),
    ("provenance.version", "simulation-v2"), ("provenance.content_hash", "0" * 64),
])
def test_trusted_compares_every_field(field, value):
    item = catalog(Zone.GREEN)[0]
    obj = item.provenance if field.startswith("provenance.") else item
    setattr(obj, field.rsplit(".", 1)[-1], value)
    assert not trusted(item)


@pytest.mark.parametrize("query", ["四时养生 法于阴阳", "素问 上古天真论", "四时养生", "恬淡虚无",
                                   "请问《素问》的恬淡虚无原文出处？", "模拟公开文化FAQ免责"])
def test_public_topic_allowlist(query):
    assert public_query(query)
    hits = search(query, 4, Zone.GREEN)
    assert hits and all(trusted(e) and e.zone == Zone.GREEN for e in hits)


@pytest.mark.parametrize("query", ["", "未知星系数据库", "的", "阴", "天", "四时养生 用户秘密",
    "素问 手机号13800138000", "恬淡虚无 病历", "素问 private record", "素问身份证",
    "四时养生 api_key", "素问 https://example.com", "恬淡虚无 alice@example.com",
    "四时养生 张三", "四时养生 未公开材料", "素问个人资料", "素问密码", "四时养生患者",
    "素问秘密" + "x" * 2000])
def test_public_query_never_infers_safety_from_topic(query):
    assert not public_query(query)
    if 0 < len(query) <= 2000:
        assert search(query, 4, Zone.GREEN) == []


@pytest.mark.parametrize("zone", list(Zone))
@pytest.mark.parametrize("query", ["的", "阴", "未知星系数据库", "流程"])
def test_lexical_no_fallback_or_single_character(zone, query):
    assert search(query, 4, zone) == []


@pytest.mark.parametrize("top_k", [0, 21, -1, True, "4"])
def test_search_bound_top_k(top_k):
    with pytest.raises(ValueError):
        search("四时养生", top_k, Zone.GREEN)


@pytest.mark.parametrize("app", [public_app, green_app, red_app])
def test_retrieval_internal_auth_and_bounds(app):
    client = TestClient(app)
    payload = {"query": "虚构流程甲" if app is red_app else "四时养生", "top_k": 1}
    assert client.post("/retrieve", json=payload).status_code == 401
    assert client.post("/retrieve", json=payload, headers={"X-Internal-Key": "bad"}).status_code == 401
    result = client.post("/retrieve", json=payload, headers=HEADERS)
    assert result.status_code == 200 and len(result.json()) == 1
    for invalid in ({"top_k": 0}, {"top_k": 21}, {"query": ""}, {"query": "x" * 2001}):
        assert client.post("/retrieve", json={**payload, **invalid}, headers=HEADERS).status_code == 422
    assert client.post("/retrieve", json={"query": "未知星系数据库"}, headers=HEADERS).json() == []


@pytest.mark.parametrize("app,path,payload", [
    (local_app, "/infer", request_payload()), (tuned_app, "/reason", request_payload(Zone.RED)),
    (gate_app, "/gate", candidate().model_dump(mode="json")),
    (hybrid_app, "/dispatch", {"query": "四时养生", "zone": "green"}),
])
def test_all_local_business_routes_require_auth(app, path, payload):
    assert TestClient(app).post(path, json=payload).status_code == 401


def test_missing_server_key_fails_closed(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    get_settings.cache_clear()
    assert TestClient(green_app).post("/retrieve", json={"query": "四时养生"}, headers=HEADERS).status_code == 503


def test_local_stub_is_exact_bounded_and_never_network(monkeypatch):
    mock_http(monkeypatch, lambda req: pytest.fail("stub attempted network"))
    response = TestClient(local_app).post("/infer", json=request_payload(), headers=HEADERS)
    assert response.status_code == 200
    cand = Candidate.model_validate(response.json())
    assert cand.model == "local-extract-simulation" and len(cand.answer) <= 4096
    assert "未调用真实模型" in cand.answer and DISCLAIMER in cand.answer
    assert supported_quotes(cand.answer, cand.evidence)


@pytest.mark.parametrize("mutation,status", [
    ("red", 403), ("unknown", 403), ("tamper", 403), ("red_evidence", 403),
    ("missing_zone", 403), ("no_evidence", 422), ("sensitive_query", 403), ("long_task_id", 413),
])
def test_local_rejects_invalid_evidence_before_ollama(monkeypatch, mutation, status):
    monkeypatch.setenv("LOCAL_MODEL_BACKEND", "ollama")
    get_settings.cache_clear()
    payload = request_payload()
    if mutation in ("red", "unknown"):
        payload["zone"] = mutation
    elif mutation == "tamper":
        payload["evidence"][0]["provenance"]["quote"] += "伪造"
    elif mutation == "red_evidence":
        payload["evidence"] = request_payload(Zone.RED)["evidence"]
    elif mutation == "missing_zone":
        payload["evidence"][0].pop("zone")
    elif mutation == "no_evidence":
        payload["evidence"] = []
    elif mutation == "sensitive_query":
        payload["query"] = "四时养生 我的病历"
    else:
        payload["task_id"] = "x" * 129
    mock_http(monkeypatch, lambda req: pytest.fail("invalid evidence reached Ollama"))
    assert TestClient(local_app).post("/infer", json=payload, headers=HEADERS).status_code == status


def test_ollama_real_adapter_bounded_request(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_BACKEND", "ollama")
    get_settings.cache_clear()
    calls = []
    def handler(req):
        calls.append(req)
        assert str(req.url) == "http://ollama:11434/api/generate"
        data = json.loads(req.content)
        assert data["stream"] is False and data["model"] == "test-local-model"
        assert data["options"]["num_predict"] <= 512 and data["options"]["num_ctx"] == 4096
        assert len(data["prompt"].encode()) <= 8000
        assert all(timeout <= 120 for timeout in req.extensions["timeout"].values())
        expected = json.loads(data["prompt"].split("\n", 1)[1])["expected_output"]
        return httpx.Response(200, json={"response": expected, "done": True})
    mock_http(monkeypatch, handler)
    response = TestClient(local_app).post("/infer", json=request_payload(), headers=HEADERS)
    assert response.status_code == 200 and len(calls) == 1
    assert response.json()["model"] == "ollama:test-local-model"
    assert OLLAMA_LABEL in response.json()["answer"]
    assert "未调用真实模型" not in response.json()["answer"]


@pytest.mark.parametrize("failure", ["timeout", "http", "redirect", "invalid_json", "oversize",
                                    "hallucination", "incomplete", "missing_response", "error"])
def test_ollama_failures_are_503_no_stub_success(monkeypatch, failure):
    monkeypatch.setenv("LOCAL_MODEL_BACKEND", "ollama")
    get_settings.cache_clear()
    def handler(req):
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=req)
        if failure == "http":
            return httpx.Response(500)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://example.com"})
        if failure == "invalid_json":
            return httpx.Response(200, content=b"not json")
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * 32769)
        if failure == "hallucination":
            return httpx.Response(200, json={"response": "无证据的处方", "done": True})
        if failure == "incomplete":
            return httpx.Response(200, json={"response": "", "done": False})
        if failure == "missing_response":
            return httpx.Response(200, json={"done": True})
        return httpx.Response(200, json={"error": "bad model", "done": True})
    mock_http(monkeypatch, handler)
    response = TestClient(local_app).post("/infer", json=request_payload(), headers=HEADERS)
    assert response.status_code == 503
    assert "answer" not in response.json()


@pytest.mark.parametrize("url", ["https://example.com", "http://8.8.8.8:11434",
    "http://ollama:11434/api/generate", "http://user:pass@ollama:11434", "http://ollama:11434?x=1",
    "http://ollama:11434#x", "file:///tmp/model", "http://0.0.0.0:11434"])
def test_ollama_uncontrolled_urls_blocked_before_network(monkeypatch, url):
    monkeypatch.setenv("LOCAL_MODEL_BACKEND", "ollama")
    monkeypatch.setenv("OLLAMA_URL", url)
    get_settings.cache_clear()
    mock_http(monkeypatch, lambda req: pytest.fail("uncontrolled endpoint called"))
    assert TestClient(local_app).post("/infer", json=request_payload(), headers=HEADERS).status_code == 503


def test_unknown_backend_is_503(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_BACKEND", "fake")
    get_settings.cache_clear()
    assert TestClient(local_app).post("/infer", json=request_payload(), headers=HEADERS).status_code == 503


def test_tuned_synthetic_local_only(monkeypatch):
    mock_http(monkeypatch, lambda req: pytest.fail("tuned simulation called network"))
    result = TestClient(tuned_app).post("/reason", json=request_payload(Zone.RED), headers=HEADERS)
    assert result.status_code == 200
    cand = Candidate.model_validate(result.json())
    assert cand.from_zone == Zone.RED and "非真实训练模型" in cand.answer
    assert cand.model == "daobrain-untrained-simulation" and supported_quotes(cand.answer, cand.evidence)


@pytest.mark.parametrize("mutation", ["green", "unknown", "tamper", "green_evidence", "missing_zone"])
def test_tuned_rejects_non_synthetic_or_non_red(monkeypatch, mutation):
    payload = request_payload(Zone.RED)
    if mutation in ("green", "unknown"):
        payload["zone"] = mutation
    elif mutation == "tamper":
        payload["evidence"][0]["text"] += "真实秘密"
    elif mutation == "green_evidence":
        payload["evidence"] = request_payload()["evidence"]
    else:
        payload["evidence"][0].pop("zone")
    mock_http(monkeypatch, lambda req: pytest.fail("tuned attempted network"))
    assert TestClient(tuned_app).post("/reason", json=payload, headers=HEADERS).status_code == 403


def test_red_gate_denies_client_approval_without_leaking_audit(caplog, monkeypatch):
    cand = candidate(Zone.RED)
    cand.task_id = "private-client-input"
    payload = {**cand.model_dump(mode="json"), "gate_approved": True, "approved": True}
    # Shared audit deliberately disables propagation; restore it only in this test.
    monkeypatch.setattr(logging.getLogger("daobrain.audit"), "propagate", True)
    with caplog.at_level(logging.INFO, logger="daobrain.audit"):
        response = TestClient(gate_app).post("/gate", json=payload, headers=HEADERS)
    assert response.status_code == 403 and "独立人工复核" in response.text
    assert "independent_manual_review_required" in caplog.text
    assert cand.task_id not in caplog.text and cand.answer not in caplog.text
    assert all(e.text not in caplog.text for e in cand.evidence)


@pytest.mark.parametrize("mutation", ["unknown", "forged_green", "tamper", "unsupported", "unused", "empty"])
def test_gate_fail_closed(mutation):
    cand = candidate(Zone.RED) if mutation == "forged_green" else candidate()
    if mutation == "unknown":
        cand.from_zone = Zone.UNKNOWN
    elif mutation == "forged_green":
        cand.from_zone = Zone.GREEN
        for e in cand.evidence:
            e.zone = Zone.GREEN
    elif mutation == "tamper":
        cand.evidence[0].provenance.content_hash = "fake"
    elif mutation == "unsupported":
        cand.answer += "\n另有无根据的断言"
    elif mutation == "unused":
        cand.evidence.append(catalog(Zone.GREEN)[2])
    else:
        cand.evidence = []
    assert TestClient(gate_app).post("/gate", json=cand.model_dump(mode="json"), headers=HEADERS).status_code == 403


def test_gate_passes_only_existing_supported_green():
    cand = candidate()
    result = TestClient(gate_app).post("/gate", json=cand.model_dump(mode="json"), headers=HEADERS)
    assert result.status_code == 200 and result.json() == cand.model_dump(mode="json")


@pytest.fixture
def local_mesh(monkeypatch):
    clients = {"rag_green": TestClient(green_app), "rag_red": TestClient(red_app),
               "local_model": TestClient(local_app), "daobrain_tuned": TestClient(tuned_app),
               "redline_gate": TestClient(gate_app), "output_verify": TestClient(verify_app)}
    calls = []
    overrides = {}
    def handler(req):
        host = req.url.host
        calls.append(host)
        assert host not in ("rag_public", "external_model")
        assert req.headers.get("X-Internal-Key") == HEADERS["X-Internal-Key"]
        if host in overrides:
            return overrides[host](req)
        assert host in clients, f"unexpected destination: {req.url}"
        response = clients[host].post(req.url.path, json=json.loads(req.content), headers=HEADERS)
        return httpx.Response(response.status_code, content=response.content,
                              headers={"Content-Type": "application/json"})
    mock_http(monkeypatch, handler)
    return calls, overrides


def assert_refused(response):
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["refused"] is True and data["answer"] == "" and data["evidence"] == []
    assert data["verification"]["status"] == "reject"
    return data


def test_integrated_green_local_only(local_mesh):
    calls, _ = local_mesh
    response = TestClient(hybrid_app).post("/dispatch", json={
        "task_id": "green-flow", "query": "四时养生 法于阴阳", "zone": "green", "gate_approved": True,
    }, headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()
    assert not data["refused"] and data["answer"] and data["evidence"]
    assert data["verification"]["status"] == "pass"
    assert calls == ["rag_green", "local_model", "output_verify"]
    assert data["trace"] == ["hybrid_iface", *calls]
    assert data["simulated"] is True and data["model"] == "local-extract-simulation"


def test_integrated_red_gate_denial_no_public_external_or_verifier(local_mesh):
    calls, _ = local_mesh
    response = TestClient(hybrid_app).post("/dispatch", json={
        "task_id": "red-flow", "query": "虚构流程甲", "zone": "red", "gate_approved": True,
    }, headers=HEADERS)
    data = assert_refused(response)
    assert calls == ["rag_red", "daobrain_tuned", "redline_gate"]
    assert data["trace"] == ["hybrid_iface", *calls]
    assert "独立人工复核" in data["note"] and "虚构流程" not in response.text


@pytest.mark.parametrize("zone,query", [("unknown", "四时养生"), ("green", "素问 秘密"),
                                        ("green", "未知星系数据库")])
def test_hybrid_unknown_or_non_public_calls_nothing(local_mesh, zone, query):
    calls, _ = local_mesh
    assert_refused(TestClient(hybrid_app).post("/dispatch", json={"query": query, "zone": zone}, headers=HEADERS))
    assert calls == []


@pytest.mark.parametrize("zone,query,rag", [("green", "四时养生", "rag_green"), ("red", "未命中星系", "rag_red")])
def test_hybrid_no_hit_never_falls_back(local_mesh, zone, query, rag):
    calls, overrides = local_mesh
    overrides[rag] = lambda req: httpx.Response(200, json=[])
    assert_refused(TestClient(hybrid_app).post("/dispatch", json={"query": query, "zone": zone}, headers=HEADERS))
    assert calls == [rag]


@pytest.mark.parametrize("zone", ["green", "red"])
def test_hybrid_tampered_retrieval_no_inference(local_mesh, zone):
    calls, overrides = local_mesh
    item = catalog(Zone(zone))[0].model_dump(mode="json")
    item["provenance"]["quote"] += "tamper"
    overrides["rag_" + zone] = lambda req: httpx.Response(200, json=[item])
    assert_refused(TestClient(hybrid_app).post("/dispatch", json={
        "query": "四时养生 法于阴阳" if zone == "green" else "虚构流程甲", "zone": zone}, headers=HEADERS))
    assert calls == ["rag_" + zone]


@pytest.mark.parametrize("mutation", ["answer", "task_id", "zone", "evidence", "model"])
def test_hybrid_rejects_tampered_candidate_before_verification(local_mesh, mutation):
    calls, overrides = local_mesh
    def fake_infer(req):
        data = json.loads(req.content)
        cand = candidate()
        cand.task_id = data["task_id"]
        payload = cand.model_dump(mode="json")
        if mutation == "answer":
            payload["answer"] += "\n无依据的结论"
        elif mutation == "task_id":
            payload["task_id"] = "different-task"
        elif mutation == "zone":
            payload["from_zone"] = "red"
        elif mutation == "evidence":
            payload["evidence"][0]["provenance"]["quote"] = "bad"
        else:
            payload["model"] = "fake-success"
        return httpx.Response(200, json=payload)
    overrides["local_model"] = fake_infer
    assert_refused(TestClient(hybrid_app).post("/dispatch", json={
        "task_id": "test", "query": "四时养生 法于阴阳", "zone": "green"}, headers=HEADERS))
    assert "output_verify" not in calls


@pytest.mark.parametrize("status,task_id,provenance,compliance", [
    ("revise", "test", True, True), ("reject", "test", True, True),
    ("pass", "wrong-task", True, True), ("pass", "test", False, True), ("pass", "test", True, False),
])
def test_hybrid_only_verified_pass_returns_answer(local_mesh, status, task_id, provenance, compliance):
    calls, overrides = local_mesh
    overrides["output_verify"] = lambda req: httpx.Response(200, json={
        "task_id": task_id, "status": status, "provenance_ok": provenance, "compliance_ok": compliance,
    })
    assert_refused(TestClient(hybrid_app).post("/dispatch", json={
        "task_id": "test", "query": "四时养生", "zone": "green"}, headers=HEADERS))
    assert calls == ["rag_green", "local_model", "output_verify"]


def test_hybrid_compromised_gate_cannot_release_red(local_mesh):
    calls, overrides = local_mesh
    def fake_gate(req):
        data = json.loads(req.content)
        data["from_zone"] = "green"
        return httpx.Response(200, json=data)
    overrides["redline_gate"] = fake_gate
    assert_refused(TestClient(hybrid_app).post("/dispatch", json={
        "query": "虚构流程甲", "zone": "red", "gate_approved": True}, headers=HEADERS))
    assert calls == ["rag_red", "daobrain_tuned", "redline_gate"]


@pytest.mark.parametrize("failure", ["timeout", "http", "invalid", "oversize", "redirect", "auth"])
def test_hybrid_errors_are_controlled_503(local_mesh, failure):
    calls, overrides = local_mesh
    def fail(req):
        if failure == "timeout":
            raise httpx.ReadTimeout("unavailable", request=req)
        if failure == "http":
            return httpx.Response(500)
        if failure == "invalid":
            return httpx.Response(200, json={"not": "list"})
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * 131073)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://example.com"})
        return httpx.Response(401)
    overrides["rag_green"] = fail
    response = TestClient(hybrid_app).post("/dispatch", json={"query": "四时养生", "zone": "green"}, headers=HEADERS)
    assert response.status_code == 503 and "answer" not in response.json()
    assert calls == ["rag_green"]


def test_hybrid_zero_budget_prevents_calls(local_mesh):
    calls, _ = local_mesh
    assert_refused(TestClient(hybrid_app).post("/dispatch", json={
        "query": "四时养生", "zone": "green", "budget_cny": "0"}, headers=HEADERS))
    assert calls == []


def test_hybrid_input_budget_bound(monkeypatch, local_mesh):
    calls, _ = local_mesh
    monkeypatch.setenv("MAX_TOKENS_PER_TASK", "1")
    get_settings.cache_clear()
    assert_refused(TestClient(hybrid_app).post("/dispatch", json={"query": "四时养生", "zone": "green"}, headers=HEADERS))
    assert calls == []


@pytest.mark.parametrize("invalid", [{"budget_cny": "-1"}, {"budget_cny": "NaN"}, {"budget_cny": "Infinity"},
                                     {"max_iterations": 0}, {"max_iterations": 7}, {"top_k": 21}, {"query": "x" * 2001}])
def test_hybrid_bounded_task_schema(invalid, local_mesh):
    calls, _ = local_mesh
    response = TestClient(hybrid_app).post("/dispatch", json={"query": "四时养生", "zone": "green", **invalid}, headers=HEADERS)
    assert response.status_code == 422 and calls == []
