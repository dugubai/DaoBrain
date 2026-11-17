"""Individual application simulations and their catalog/portal contracts.

Keys and state are installed only inside fixtures, never during collection.
No network service, persistent database, image decoder or vector store is used.
"""
import importlib
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def simulation_settings(monkeypatch):
    from common.config import Settings, get_settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("API_KEY", "application-test-user")
    monkeypatch.setenv("INTERNAL_API_KEY", "application-test-reviewer")
    monkeypatch.setenv("APPROVAL_SECRET", "application-test-approval")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def clients(monkeypatch):
    modules = {name: importlib.import_module(f"{name}.main")
               for name in ("corpus_kb", "card_annotator", "ocr")}
    monkeypatch.setattr(modules["card_annotator"], "_cards", {})
    for module in modules.values():
        module.app.dependency_overrides.clear()
    instances = {name: TestClient(module.app) for name, module in modules.items()}
    yield instances
    for client in instances.values():
        client.close()
    for module in modules.values():
        module.app.dependency_overrides.clear()


@pytest.fixture
def api_headers():
    return {"X-API-Key": "application-test-user"}


@pytest.fixture
def internal_headers():
    return {"X-Internal-Key": "application-test-reviewer"}


@pytest.fixture
def green():
    from common.catalog import catalog
    from common.schemas import Zone
    return catalog(Zone.GREEN)[0].model_dump(mode="json")


@pytest.fixture
def red():
    from common.catalog import catalog
    from common.schemas import Zone
    return catalog(Zone.RED)[0].model_dump(mode="json")


def create(client, headers, payload):
    response = client.post("/cards", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def review_payload(card, decision="approved", **changes):
    return {"card_id": card["card_id"], "expected_version": card["version"],
            "decision": decision, **changes}


AUTH_ENDPOINTS = [
    ("corpus_kb", "POST", "/search", {"query": "法于阴阳"}, "internal"),
    ("corpus_kb", "GET", "/sources/missing", None, "internal"),
    ("card_annotator", "POST", "/cards", {"text": "sample"}, "api"),
    ("card_annotator", "GET", "/cards", None, "api"),
    ("card_annotator", "PATCH", "/cards/missing", {"text": "sample", "expected_version": 1}, "api"),
    ("card_annotator", "POST", "/review", {"card_id": "a" * 32, "expected_version": 1, "decision": "rejected"}, "internal"),
    ("ocr", "POST", "/recognize", {"mode": "text", "text": "sample"}, "internal"),
]


@pytest.mark.parametrize("module,method,path,payload,role", AUTH_ENDPOINTS)
@pytest.mark.parametrize("supplied", ["missing", "invalid", "opposite-role"])
def test_business_endpoints_require_correct_role(clients, module, method, path, payload,
                                                role, supplied, api_headers, internal_headers):
    headers = {} if supplied == "missing" else (
        {"X-API-Key" if role == "api" else "X-Internal-Key": "incorrect"}
        if supplied == "invalid" else (internal_headers if role == "api" else api_headers))
    response = clients[module].request(method, path, headers=headers, json=payload)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("module,method,path,payload,role", AUTH_ENDPOINTS)
def test_unconfigured_keys_fail_closed(clients, monkeypatch, module, method, path, payload,
                                      role, api_headers, internal_headers):
    from common.config import get_settings
    monkeypatch.setenv("API_KEY" if role == "api" else "INTERNAL_API_KEY", "")
    get_settings.cache_clear()
    response = clients[module].request(method, path, json=payload,
                                       headers=api_headers if role == "api" else internal_headers)
    assert response.status_code == 503, response.text


@pytest.mark.parametrize("module", ["corpus_kb", "card_annotator", "ocr"])
def test_health_is_process_only_and_not_auth_proof(clients, module, monkeypatch):
    from common.config import get_settings
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    get_settings.cache_clear()
    assert clients[module].get("/health").json() == {"status": "ok", "module": module}


def test_corpus_search_and_exact_lookup(clients, internal_headers):
    from common.catalog import trusted
    from common.schemas import Retrieved
    corpus = clients["corpus_kb"]
    response = corpus.post("/search", headers=internal_headers,
                           json={"query": "法于阴阳，和于术数", "top_k": 1})
    assert response.status_code == 200
    hits = response.json()
    assert len(hits) == 1
    assert hits[0]["zone"] == "green" and trusted(Retrieved(**hits[0]))
    provenance = hits[0]["provenance"]
    assert len(provenance["content_hash"]) == 64 and provenance["version"]
    path = "/sources/" + provenance["source_id"]
    lookup = corpus.get(path, headers=internal_headers,
                        params={"version": provenance["version"], "content_hash": provenance["content_hash"]})
    assert lookup.status_code == 200 and lookup.json() == hits[0]
    for params in ({"version": "not-the-version"}, {"content_hash": "0" * 64}):
        assert corpus.get(path, headers=internal_headers, params=params).status_code == 404


@pytest.mark.parametrize("query", ["完全不相关的主题", "法于阴阳与内部秘密", "合成红区", "a"])
def test_corpus_no_fallback_or_red_query(clients, internal_headers, query):
    response = clients["corpus_kb"].post("/search", headers=internal_headers, json={"query": query})
    assert response.status_code == 200 and response.json() == []


def test_corpus_unknown_and_red_source_not_exposed(clients, internal_headers, red):
    for source_id in ("unknown-source", red["provenance"]["source_id"]):
        response = clients["corpus_kb"].get("/sources/" + source_id, headers=internal_headers)
        assert response.status_code == 404
    assert clients["corpus_kb"].post("/upload", headers=internal_headers, json=red).status_code == 404


@pytest.mark.parametrize("payload", [
    {"query": ""}, {"query": "x" * 2001}, {"query": "素问", "top_k": 0},
    {"query": "素问", "top_k": 21}, {"query": "素问", "zone": "red"},
    {"query": "素问", "corpus": [{"text": "private"}]},
])
def test_corpus_request_bounds_and_no_upload(clients, internal_headers, payload):
    assert clients["corpus_kb"].post("/search", headers=internal_headers, json=payload).status_code == 422


def test_cards_full_deterministic_lifecycle(clients, api_headers, internal_headers, green):
    client = clients["card_annotator"]
    draft = create(client, api_headers, green)
    assert re.fullmatch(r"[a-f0-9]{32}", draft["card_id"])
    assert draft["status"] == "draft" and draft["version"] == 1
    assert draft["simulated"] is True and "in-memory" in draft["storage"]
    assert draft["created_by"] == "simulation-user"
    approved_response = client.post("/review", headers=internal_headers, json=review_payload(draft))
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    assert (approved["status"], approved["version"]) == ("approved", 2)
    assert approved["reviewed_by"] != approved["created_by"]
    assert client.post("/review", headers=internal_headers, json=review_payload(draft)).status_code == 409
    assert client.post("/review", headers=internal_headers, json=review_payload(approved)).status_code == 409
    stale_revision = client.patch("/cards/" + draft["card_id"], headers=api_headers,
                                  json={**green, "expected_version": 1})
    assert stale_revision.status_code == 409
    revision_response = client.patch("/cards/" + draft["card_id"], headers=api_headers,
                                     json={**green, "annotation": "Review again", "expected_version": 2})
    assert revision_response.status_code == 200, revision_response.text
    revision = revision_response.json()
    assert (revision["status"], revision["version"]) == ("revision", 3)
    assert revision["reviewed_by"] is None
    assert client.post("/review", headers=internal_headers,
                       json=review_payload(approved, "rejected")).status_code == 409
    rejected_response = client.post("/review", headers=internal_headers,
                                    json=review_payload(revision, "rejected", comment="Revise annotation"))
    assert rejected_response.status_code == 200, rejected_response.text
    rejected = rejected_response.json()
    assert (rejected["status"], rejected["version"]) == ("rejected", 4)
    second_revision = client.patch("/cards/" + draft["card_id"], headers=api_headers,
                                   json={**green, "expected_version": 4}).json()
    assert (second_revision["status"], second_revision["version"]) == ("revision", 5)
    final_response = client.post("/review", headers=internal_headers, json=review_payload(second_revision))
    assert final_response.status_code == 200, final_response.text
    assert (final_response.json()["status"], final_response.json()["version"]) == ("approved", 6)
    listing = client.get("/cards", headers=api_headers).json()
    assert listing["total"] == 1 and listing["cards"] == [final_response.json()]


def test_draft_cannot_skip_review_into_revision(clients, api_headers, green):
    client = clients["card_annotator"]
    card = create(client, api_headers, green)
    assert client.patch("/cards/" + card["card_id"], headers=api_headers,
                        json={**green, "expected_version": 1}).status_code == 409


def test_authenticated_self_review_is_rejected(clients, api_headers, internal_headers, green):
    from card_annotator.main import app
    from common.security import require_api
    client = clients["card_annotator"]
    app.dependency_overrides[require_api] = lambda: "simulation-reviewer"
    try:
        card = create(client, api_headers, green)
    finally:
        app.dependency_overrides.pop(require_api)
    response = client.post("/review", headers=internal_headers, json=review_payload(card))
    assert response.status_code == 403 and "Self-review" in response.json()["detail"]
    assert client.get("/cards", headers=api_headers).json()["cards"][0]["version"] == 1


def test_revision_requires_authenticated_owner(clients, api_headers, internal_headers, green):
    from card_annotator.main import app
    from common.security import require_api
    client = clients["card_annotator"]
    card = create(client, api_headers, green)
    assert client.post("/review", headers=internal_headers,
                       json=review_payload(card, "rejected")).status_code == 200
    app.dependency_overrides[require_api] = lambda: "another-simulation-user"
    try:
        response = client.patch("/cards/" + card["card_id"], headers=api_headers,
                                json={**green, "expected_version": 2})
    finally:
        app.dependency_overrides.pop(require_api)
    assert response.status_code == 403


@pytest.mark.parametrize("decision", ["approved", "rejected"])
@pytest.mark.parametrize("missing", ["all", "quote", "version", "content_hash"])
def test_review_requires_complete_provenance(clients, api_headers, internal_headers, green, decision, missing):
    payload = {**green, "provenance": None if missing == "all" else {**green["provenance"], missing: ""}}
    client = clients["card_annotator"]
    card = create(client, api_headers, payload)
    assert client.post("/review", headers=internal_headers,
                       json=review_payload(card, decision)).status_code == 422


@pytest.mark.parametrize("zone", ["red", "unknown"])
def test_non_green_approval_is_blocked(clients, api_headers, internal_headers, red, zone):
    client = clients["card_annotator"]
    card = create(client, api_headers, {**red, "zone": zone})
    assert client.post("/review", headers=internal_headers,
                       json=review_payload(card, target_zone="green")).status_code == 403
    assert client.get("/cards", headers=api_headers).json()["cards"][0]["status"] == "draft"


def test_red_cannot_be_relabelled_after_rejection(clients, api_headers, internal_headers, red):
    client = clients["card_annotator"]
    card = create(client, api_headers, red)
    assert client.post("/review", headers=internal_headers,
                       json=review_payload(card, "rejected")).status_code == 200
    assert client.patch("/cards/" + card["card_id"], headers=api_headers,
                        json={**red, "zone": "green", "expected_version": 2}).status_code == 403


@pytest.mark.parametrize("tamper", ["text", "title", "quote", "version", "hash", "red-labelled-green"])
def test_forged_green_approval_is_blocked(clients, api_headers, internal_headers, green, red, tamper):
    if tamper == "red-labelled-green":
        payload = {**red, "zone": "green"}
    elif tamper == "text":
        payload = {**green, "text": "Untrusted red content appended to public sample"}
    else:
        field = "content_hash" if tamper == "hash" else tamper
        value = "0" * 64 if field == "content_hash" else "forged provenance"
        payload = {**green, "provenance": {**green["provenance"], field: value}}
    client = clients["card_annotator"]
    card = create(client, api_headers, payload)
    assert client.post("/review", headers=internal_headers, json=review_payload(card)).status_code == 403


@pytest.mark.parametrize("annotation_kind", ["unverified", "trusted"])
def test_annotations_cannot_launder_unverified_prose(clients, api_headers, internal_headers, green, red,
                                                   annotation_kind):
    annotation = red["text"] if annotation_kind == "unverified" else green["provenance"]["quote"]
    client = clients["card_annotator"]
    card = create(client, api_headers, {**green, "annotation": annotation})
    response = client.post("/review", headers=internal_headers, json=review_payload(card))
    assert response.status_code == (403 if annotation_kind == "unverified" else 200)


@pytest.mark.parametrize("extra", [{"card_id": "../../owned"}, {"created_by": "reviewer"},
                                  {"status": "approved"}, {"version": 900}, {"path": "/tmp/file"}])
def test_card_ids_roles_status_are_server_owned(clients, api_headers, green, extra):
    assert clients["card_annotator"].post("/cards", headers=api_headers,
                                         json={**green, **extra}).status_code == 422


def test_reviewer_cannot_supply_identity(clients, api_headers, internal_headers, green):
    client = clients["card_annotator"]
    card = create(client, api_headers, green)
    assert client.post("/review", headers=internal_headers,
                       json=review_payload(card, reviewer_id="someone-else")).status_code == 422


def test_cards_unknown_id_and_bounded_state(clients, monkeypatch, api_headers, internal_headers, green):
    from card_annotator import main
    client = clients["card_annotator"]
    assert client.post("/review", headers=internal_headers,
                       json={"card_id": "f" * 32, "expected_version": 1, "decision": "rejected"}).status_code == 404
    monkeypatch.setattr(main, "MAX_CARDS", 2)
    first = create(client, api_headers, green)
    second = create(client, api_headers, green)
    assert first["card_id"] != second["card_id"]
    assert client.post("/cards", headers=api_headers, json=green).status_code == 409
    listing = client.get("/cards?offset=1&limit=1", headers=api_headers).json()
    assert listing["total"] == 2 and listing["cards"][0]["card_id"] == second["card_id"]
    for path in ("/cards?limit=51", "/cards?limit=0", "/cards?offset=-1"):
        assert client.get(path, headers=api_headers).status_code == 422


@pytest.mark.parametrize("payload", [{"text": ""}, {"text": "   "}, {"text": "x" * 4001},
                                     {"text": "sample", "annotation": "x" * 2001},
                                     {"text": "sample", "provenance": {"source_id": "s", "title": "t", "quote": "x" * 4001}}])
def test_card_payload_bounds(clients, api_headers, payload):
    assert clients["card_annotator"].post("/cards", headers=api_headers, json=payload).status_code == 422


def test_ocr_explicit_typed_text_simulation(clients, internal_headers):
    payload = {"mode": "text", "text": "法于阴阳\n\n和于术数", "page": 3}
    response = clients["ocr"].post("/recognize", headers=internal_headers, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "simulation" and result["simulated"] and result["needs_review"]
    assert result["page"] == 3
    assert [s["text"] for s in result["segments"]] == ["法于阴阳", "和于术数"]
    assert all(s["page"] == 3 and s["synthetic"] and s["needs_review"] for s in result["segments"])
    assert "OCR" in result["note"] and "no image" in result["note"]
    assert "confidence" not in result and "bbox" not in result


@pytest.mark.parametrize("payload", [
    {"mode": "image", "image": "not-real-image"}, {"image_base64": "eA=="},
    {"base64": "eA=="}, {"mode": "base64", "text": "eA=="},
    {"mode": "text", "text": "hello", "image": "eA=="},
    {"image_url": "https://example.invalid/image.png"},
])
def test_ocr_image_requests_honestly_unimplemented(clients, internal_headers, payload):
    response = clients["ocr"].post("/recognize", headers=internal_headers, json=payload)
    assert response.status_code == 501 and "not implemented" in response.json()["detail"]


@pytest.mark.parametrize("payload", [
    {"text": "implicit mode is not allowed"}, {"mode": "text", "text": ""},
    {"mode": "text", "text": "  "}, {"mode": "text", "text": "x" * 10001},
    {"mode": "text", "text": "line\n" * 201}, {"mode": "text", "text": "ok", "page": 0},
    {"mode": "text", "text": "ok", "page": 10001}, {"mode": "text", "text": "ok", "page": "1"},
    {"mode": "text", "text": "ok", "path": "/tmp/image"},
    {"mode": "text", "text": "ok", "url": "https://example.invalid"},
])
def test_ocr_bounds_and_no_path_or_url_ingestion(clients, internal_headers, payload):
    assert clients["ocr"].post("/recognize", headers=internal_headers, json=payload).status_code == 422


def test_catalog_search_to_card_review_does_not_publish(clients, api_headers, internal_headers):
    corpus = clients["corpus_kb"]
    query = {"query": "法于阴阳", "top_k": 1}
    before = corpus.post("/search", headers=internal_headers, json=query).json()
    card = create(clients["card_annotator"], api_headers, before[0])
    reviewed = clients["card_annotator"].post("/review", headers=internal_headers,
                                            json=review_payload(card))
    assert reviewed.status_code == 200
    after = corpus.post("/search", headers=internal_headers, json=query).json()
    assert before == after
    recognized = clients["ocr"].post("/recognize", headers=internal_headers,
                                     json={"mode": "text", "text": before[0]["text"]}).json()
    assert recognized["needs_review"] and recognized["status"] == "simulation"


def portal_html():
    return (Path(__file__).resolve().parents[1] / "portal" / "index.html").read_text(encoding="utf-8")


def test_portal_same_origin_no_storage_or_unsafe_html():
    html = portal_html()
    assert 'type="password"' in html and 'autocomplete="off"' in html
    assert 'fetch("/v1/query"' in html and '"X-API-Key": apiKey' in html
    assert "SIMULATED ONLY" in html and "禁止用于临床" in html
    assert "textContent" in html and "replaceChildren" in html
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "localStorage",
                      "sessionStorage", "indexedDB", "document.cookie", "console.", "<script src="):
        assert forbidden not in html
    assert 'redirect: "error"' in html


def test_portal_javascript_form_and_untrusted_rendering():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable for dependency-free portal smoke test")
    script = re.search(r"<script>([\s\S]*?)</script>", portal_html()).group(1)
    harness = r'''
const assert = require("node:assert/strict");
class Element {
  constructor() { this.children = []; this.value = ""; this.textContent = ""; this.dataset = {}; this.listeners = {}; this.disabled = false; this.hidden = true; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  focus() { this.focused = true; }
  setAttribute() {}
  removeAttribute() {}
  reportValidity() { return true; }
  set innerHTML(_) { throw Error("Unsafe HTML sink"); }
}
const elements = new Map();
const examples = [new Element()];
examples[0].dataset.query = "法于阴阳";
global.document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement() { return new Element(); },
  querySelectorAll() { return examples; }
};
let httpStatus = 200;
let lastRequest;
const attack = '<img src=x onerror="alert(1)">';
global.fetch = async (path, options) => {
  lastRequest = { path, options };
  return { ok: httpStatus === 200, status: httpStatus, json: async () => ({
    answer: attack, model: "simulation-label", simulated: true, refused: false,
    verification: { status: "pass", provenance_ok: true, compliance_ok: true, reasons: [attack] },
    evidence: [{ title: attack, quote: attack, version: "simulation-v1", content_hash: "a".repeat(64) }]
  }) };
};
'''
    assertions = r'''
(async () => {
  const get = id => document.getElementById(id);
  get("api-key").value = "ephemeral-test-key";
  get("query").value = "素问";
  get("top-k").value = "4";
  examples[0].listeners.click();
  assert.equal(get("query").value, "法于阴阳");
  await get("query-form").listeners.submit({ preventDefault() {} });
  assert.equal(lastRequest.path, "/v1/query");
  assert.equal(lastRequest.options.headers["X-API-Key"], "ephemeral-test-key");
  const body = JSON.parse(lastRequest.options.body);
  assert.deepEqual(body, { query: "法于阴阳", top_k: 4, zone: "green" });
  assert(!lastRequest.options.body.includes("ephemeral-test-key"));
  assert.equal(lastRequest.options.redirect, "error");
  assert.equal(get("answer-text").textContent, attack);
  assert.equal(get("sources").children[0].children[0].textContent, attack);
  assert.equal(get("verification-reasons").children[0].textContent, attack);
  assert.equal(get("result").hidden, false);
  assert.equal(get("submit-button").disabled, false);
  httpStatus = 401;
  await get("query-form").listeners.submit({ preventDefault() {} });
  assert.equal(get("result").hidden, true);
  assert(get("request-status").textContent.includes("鉴权失败"));
  assert(!get("request-status").textContent.includes("ephemeral-test-key"));
  assert.equal(get("submit-button").disabled, false);
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", harness + script + assertions], capture_output=True,
                            text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
