"""R5 integration contracts for the future application_hub.main.app.

The hub import is deferred to a fixture, not replaced with a fabricated test app.
These tests require the hub to include all three routers. They use only a
throwaway SQLite DB, locally provisioned users/sessions, and an explicitly test-
only HMAC secret. No login throttling workarounds, network, real payment keys,
external services, or dependency installation. Not executed during authoring.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import importlib
import json
import secrets
from threading import Barrier
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from application_hub.security import NewUser, add_user, digest_token, password_hash
from application_hub.store import get, listing, transaction
from common.catalog import catalog
from common.schemas import SourceType, Zone

TEST_SECRET = "test-only-sandbox-secret-never-use-outside-tests-0123456789"
ORIGIN = "http://127.0.0.1:18121"
# Valid PBKDF2 fixture only; real login/hash verification has separate coverage.
FIXTURE_PASSWORD_HASH = password_hash("Test-only-password-123456")


def browser_client(app):
    return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ORIGIN", ORIGIN)
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "business-r5.sqlite3"))
    monkeypatch.setattr("application_hub.security.password_hash", lambda _: FIXTURE_PASSWORD_HASH)
    monkeypatch.delenv("APP_PAYMENT_MODE", raising=False)
    monkeypatch.delenv("APP_PAYMENT_SECRET", raising=False)
    users = {}
    with transaction() as db:
        for name, role in (("editor_one", "editor"), ("editor_two", "editor"),
                           ("doctor_one", "clinician"), ("doctor_two", "clinician"),
                           ("reviewer_one", "reviewer"), ("reviewer_two", "reviewer"),
                           ("member_one", "member"), ("member_two", "member"),
                           ("admin_one", "admin")):
            user = add_user(db, NewUser(name=name, password="Test-only-password-123456", role=role),
                            "test-provisioning")
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions(digest,user_id,expires) VALUES(?,?,?)",
                       (digest_token(token), user["id"], int(time.time()) + 3600))
            users[name] = {**user, "headers": {"Authorization": "Bearer " + token}}
    return users


@pytest.fixture
def client(environment):
    # Missing hub/integration is a test failure, not a skipped success or stub app.
    app = importlib.import_module("application_hub.main").app
    assert not app.dependency_overrides, "Integration tests require real session/role dependencies"
    with browser_client(app) as instance:
        yield instance


@pytest.fixture
def sandbox(client, monkeypatch):
    monkeypatch.setenv("APP_PAYMENT_MODE", "sandbox")
    monkeypatch.setenv("APP_PAYMENT_SECRET", TEST_SECRET)
    return client


@pytest.fixture
def public_source():
    return next(entry for entry in catalog(Zone.GREEN)
                if entry.provenance.source_type == SourceType.PUBLIC_CLASSIC)


def clinical_body(users, **changes):
    return {"case_id": "SYN-DEMO1", "synthetic": True, "consent": True,
            "clinician_id": users["doctor_one"]["id"], **changes}


def media_body(users, source, **changes):
    return {"source_id": source.provenance.source_id, "kind": "text",
            "reviewer_id": users["reviewer_one"]["id"], "text": source.provenance.quote, **changes}


def created(client, path, user, body):
    response = client.post(path, headers=user["headers"], json=body)
    assert response.status_code == 201, response.text
    return response.json()


def review_body(item, **changes):
    return {"expected_version": item["version"], "decision": "approved", **changes}


def event_body(order, **changes):
    return {"event_id": "evt_" + secrets.token_hex(12), "timestamp": int(time.time()),
            "event_type": "payment.paid", "order_id": order["id"],
            "amount_fen": 100, "currency": "CNY", **changes}


def encode_event(event):
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def signed_headers(body, secret=TEST_SECRET):
    return {"Content-Type": "application/json",
            "X-Payment-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}


def callback(client, event):
    body = encode_event(event)
    return client.post("/api/membership/webhook", content=body, headers=signed_headers(body))


def counts():
    with transaction() as db:
        return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                     for table in ("objects", "events", "payment_events"))


def role_change(user, role):
    with transaction() as db:
        db.execute("UPDATE users SET role=? WHERE id=?", (role, user["id"]))


AUTH_ROUTES = [
    ("POST", "/api/clinical", {}), ("GET", "/api/clinical", None),
    ("GET", "/api/clinical/" + "f" * 32, None),
    ("POST", "/api/clinical/" + "f" * 32 + "/review", {"expected_version": 1, "decision": "approved"}),
    ("POST", "/api/clinical/real", {}),
    ("POST", "/api/media", {}), ("GET", "/api/media", None),
    ("GET", "/api/media/" + "f" * 32, None),
    ("POST", "/api/media/" + "f" * 32 + "/review", {"expected_version": 1, "decision": "approved"}),
    ("POST", "/api/media/" + "f" * 32 + "/interpret", None),
    ("GET", "/api/membership/me", None), ("POST", "/api/membership/orders", {}),
    ("GET", "/api/membership/orders/" + "f" * 32, None),
]


@pytest.mark.parametrize("method,path,payload", AUTH_ROUTES)
@pytest.mark.parametrize("authorization", [None, "Bearer " + "x" * 43])
def test_person_sessions_required(client, method, path, payload, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.request(method, path, headers=headers, json=payload)
    assert response.status_code == 401, response.text


def test_expired_and_disabled_sessions(client, environment):
    member = environment["member_one"]
    with transaction() as db:
        db.execute("UPDATE sessions SET expires=0 WHERE user_id=?", (member["id"],))
    assert client.get("/api/membership/me", headers=member["headers"]).status_code == 401
    editor = environment["editor_one"]
    with transaction() as db:
        db.execute("UPDATE users SET enabled=0 WHERE id=?", (editor["id"],))
    assert client.get("/api/clinical", headers=editor["headers"]).status_code == 401


@pytest.mark.parametrize("actor", ["admin_one", "member_one", "reviewer_one"])
def test_clinical_creation_roles(client, environment, actor):
    assert client.post("/api/clinical", headers=environment[actor]["headers"],
                       json=clinical_body(environment)).status_code == 403


@pytest.mark.parametrize("actor", ["admin_one", "member_one", "doctor_one", "reviewer_one"])
def test_media_creation_roles(client, environment, public_source, actor):
    assert client.post("/api/media", headers=environment[actor]["headers"],
                       json=media_body(environment, public_source)).status_code == 403


@pytest.mark.parametrize("actor", ["admin_one", "editor_one", "doctor_one", "reviewer_one"])
def test_membership_role_is_not_admin_bypass(sandbox, environment, actor):
    headers = environment[actor]["headers"]
    assert sandbox.get("/api/membership/me", headers=headers).status_code == 403
    assert sandbox.post("/api/membership/orders", headers=headers, json={}).status_code == 403


def test_clinical_state_persistence_and_metadata_audit(client, environment):
    owner, reviewer = environment["editor_one"], environment["doctor_one"]
    draft = created(client, "/api/clinical", owner, clinical_body(environment))
    assert (draft["status"], draft["version"], draft["owner"]) == ("draft", 1, owner["id"])
    payload = draft["payload"]
    assert payload["synthetic"] is True and payload["consent"] is True
    assert set(payload["observations"]) == {"inspection", "listening", "inquiry", "palpation"}
    assert all(len(value) <= 500 for value in payload["observations"].values())
    assert sum(map(len, payload["observations"].values())) <= 1600
    path = "/api/clinical/" + draft["id"]
    assert client.get(path, headers=reviewer["headers"]).json() == draft
    stale = client.post(path + "/review", headers=reviewer["headers"],
                        json=review_body(draft, expected_version=2))
    assert stale.status_code == 409
    response = client.post(path + "/review", headers=reviewer["headers"],
                           json=review_body(draft, comment="fixture-confirmed"))
    assert response.status_code == 200, response.text
    approved = response.json()
    assert (approved["status"], approved["version"]) == ("approved", 2)
    assert approved["payload"]["review"]["reviewer_id"] == reviewer["id"] != owner["id"]
    assert client.post(path + "/review", headers=reviewer["headers"],
                       json=review_body(approved)).status_code == 409
    assert client.get("/api/clinical?status=approved", headers=owner["headers"]).json()["items"] == [approved]
    with transaction() as db:
        assert get(db, "clinical_record", draft["id"]) == approved
        actions = [dict(row) for row in db.execute("SELECT * FROM events WHERE object_id=?", (draft["id"],))]
        assert {row["action"] for row in actions} == {"clinical_record.create", "clinical_record.review.approved"}
        assert set(actions[0]) == {"id", "actor", "action", "object_id", "created"}
        assert "observations" not in json.dumps(actions)
    # A new client/connection reads the same disk-backed record; no in-memory state.
    with browser_client(client.app) as restarted:
        assert restarted.get(path, headers=owner["headers"]).json() == approved


def test_clinical_owner_and_assigned_clinician_only(client, environment):
    first = created(client, "/api/clinical", environment["editor_one"], clinical_body(environment))
    second = created(client, "/api/clinical", environment["editor_two"],
                     clinical_body(environment, clinician_id=environment["doctor_two"]["id"]))
    for actor, expected in (("editor_one", first), ("doctor_one", first),
                            ("editor_two", second), ("doctor_two", second)):
        headers = environment[actor]["headers"]
        result = client.get("/api/clinical?limit=1", headers=headers).json()
        assert result["total"] == 1 and result["items"] == [expected]
        other = second if expected == first else first
        assert client.get("/api/clinical/" + other["id"], headers=headers).status_code == 404
    path = "/api/clinical/" + first["id"] + "/review"
    assert client.post(path, headers=environment["doctor_two"]["headers"],
                       json=review_body(first)).status_code == 404
    assert client.get("/api/clinical", headers=environment["admin_one"]["headers"]).status_code == 403


def test_clinician_creation_and_self_review_rejected(client, environment):
    doctor = environment["doctor_one"]
    assert client.post("/api/clinical", headers=doctor["headers"],
                       json=clinical_body(environment)).status_code == 403
    draft = created(client, "/api/clinical", doctor,
                    clinical_body(environment, clinician_id=environment["doctor_two"]["id"]))
    path = "/api/clinical/" + draft["id"] + "/review"
    assert client.post(path, headers=doctor["headers"], json=review_body(draft)).status_code == 403
    assert client.post(path, headers=environment["doctor_two"]["headers"],
                       json=review_body(draft, decision="rejected")).json()["status"] == "rejected"


@pytest.mark.parametrize("changes", [
    {"synthetic": False}, {"consent": False}, {"synthetic": "true"}, {"consent": 1},
    {"case_id": "SYN-张三"}, {"case_id": "SYN-13800138000"}, {"case_id": "SYN-Alice"},
    {"case_id": "../records"}, {"patient_name": "张三"}, {"phone": "13800138000"},
    {"observations": {"inspection": "患者真实信息"}}, {"text": "真实病历"},
    {"diagnosis": "任意诊断"}, {"prescription": "任意处方"}, {"status": "approved"},
    {"owner": "forged-owner"}, {"version": 90}, {"role": "clinician"},
])
def test_clinical_cannot_launder_private_prose(client, environment, changes):
    before = counts()
    response = client.post("/api/clinical", headers=environment["editor_one"]["headers"],
                           json=clinical_body(environment, **changes))
    assert response.status_code == 422, response.text
    assert counts() == before


@pytest.mark.parametrize("reviewer", ["admin_one", "editor_two", "reviewer_one", "member_one"])
def test_clinical_assignment_needs_actual_clinician(client, environment, reviewer):
    response = client.post("/api/clinical", headers=environment["editor_one"]["headers"],
                           json=clinical_body(environment, clinician_id=environment[reviewer]["id"]))
    assert response.status_code == 422


def test_real_clinical_and_prose_review_are_rejected(client, environment):
    before = counts()
    response = client.post("/api/clinical/real", headers=environment["editor_one"]["headers"],
                           json={"name": "张三", "phone": "13800138000", "synthetic": True})
    assert response.status_code == 501 and counts() == before
    draft = created(client, "/api/clinical", environment["editor_one"], clinical_body(environment))
    path = "/api/clinical/" + draft["id"] + "/review"
    before = counts()
    for changes in ({"comment": "患者姓名张三，电话13800138000"}, {"reviewer_id": "forged"}):
        assert client.post(path, headers=environment["doctor_one"]["headers"],
                           json=review_body(draft, **changes)).status_code == 422
    for actor in ("admin_one", "editor_one", "reviewer_one"):
        assert client.post(path, headers=environment[actor]["headers"],
                           json=review_body(draft)).status_code == 403
    assert counts() == before


def test_four_observation_segment_and_total_bounds():
    from clinical_records.main import Observations
    base = {name: "x" for name in ("inspection", "listening", "inquiry", "palpation")}
    with pytest.raises(ValidationError):
        Observations(**{**base, "inspection": "x" * 501})
    with pytest.raises(ValidationError):
        Observations(**{key: "x" * 401 for key in base})
    assert Observations(**{key: "x" * 400 for key in base})


def test_media_review_persisted_and_text_honesty(client, environment, public_source):
    owner, reviewer = environment["editor_one"], environment["reviewer_one"]
    draft = created(client, "/api/media", owner, media_body(environment, public_source))
    path = "/api/media/" + draft["id"]
    assert draft["status"] == "draft" and draft["version"] == 1
    assert draft["payload"]["provenance"] == public_source.provenance.model_dump(mode="json")
    assert client.post(path + "/interpret", headers=owner["headers"]).status_code == 409
    assert client.post(path + "/review", headers=reviewer["headers"],
                       json=review_body(draft, expected_version=2)).status_code == 409
    response = client.post(path + "/review", headers=reviewer["headers"],
                           json=review_body(draft, comment="quote-confirmed"))
    assert response.status_code == 200, response.text
    approved = response.json()
    assert (approved["status"], approved["version"]) == ("approved", 2)
    assert approved["payload"]["review"]["reviewer_id"] != approved["owner"]
    assert client.post(path + "/review", headers=reviewer["headers"],
                       json=review_body(approved)).status_code == 409
    interpreted = client.post(path + "/interpret", headers=owner["headers"]).json()
    assert interpreted["original"] == public_source.provenance.quote
    assert interpreted["provenance"] == public_source.provenance.model_dump(mode="json")
    assert interpreted["mode"] == "deterministic-public-quotation" and interpreted["ai_used"] is False
    assert "不是AI解释" in interpreted["reading_note"]
    with transaction() as db:
        assert get(db, "media_record", draft["id"]) == approved
        assert db.execute("SELECT count(*) FROM events WHERE action='media_record.read_quote' AND object_id=?",
                          (draft["id"],)).fetchone()[0] == 1
    with browser_client(client.app) as restarted:
        assert restarted.get(path, headers=owner["headers"]).json() == approved


@pytest.mark.parametrize("kind", ["image", "video"])
def test_media_no_visual_success_stub(client, environment, public_source, kind):
    data = media_body(environment, public_source, kind=kind)
    if kind == "video":
        data["subtitles"] = public_source.provenance.quote
    draft = created(client, "/api/media", environment["editor_one"], data)
    path = "/api/media/" + draft["id"]
    assert draft["payload"]["media_asset_present"] is False
    for reviewed in (False, True):
        if reviewed:
            assert client.post(path + "/review", headers=environment["reviewer_one"]["headers"],
                               json=review_body(draft)).status_code == 200
        before = counts()
        response = client.post(path + "/interpret", headers=environment["editor_one"]["headers"])
        assert response.status_code == 501 and "未实现" in response.json()["detail"]
        assert counts() == before


@pytest.mark.parametrize("changes", [
    {"text": "张三电话13800138000"}, {"text": "法于阴阳，和于术数。额外文字"},
    {"text": "法于阴阳,和于术数"}, {"text": ""}, {"text": "x" * 4001},
    {"kind": "video", "subtitles": "私有字幕"}, {"url": "https://example.invalid/private.mp4"},
    {"image_base64": "eA=="}, {"path": "/tmp/private.png"}, {"upload": "file-content"},
    {"source_id": "synthetic-red-1"}, {"source_id": "faq-0"}, {"source_id": "unknown"},
    {"source_id": "https://example.invalid"}, {"kind": "audio"}, {"status": "approved"},
    {"owner": "forged"}, {"version": 100}, {"provenance": {"source_type": "public_classic"}},
])
def test_media_only_literal_public_classics(client, environment, public_source, changes):
    before = counts()
    response = client.post("/api/media", headers=environment["editor_one"]["headers"],
                           json=media_body(environment, public_source, **changes))
    assert response.status_code == 422, response.text
    assert counts() == before


def test_media_assignment_privacy_and_role_changes(client, environment, public_source):
    first = created(client, "/api/media", environment["editor_one"], media_body(environment, public_source))
    second = created(client, "/api/media", environment["editor_two"],
                     media_body(environment, public_source, reviewer_id=environment["reviewer_two"]["id"]))
    for actor, expected in (("editor_one", first), ("reviewer_one", first),
                            ("editor_two", second), ("reviewer_two", second)):
        headers = environment[actor]["headers"]
        result = client.get("/api/media", headers=headers).json()
        assert result["total"] == 1 and result["items"] == [expected]
        other = second if expected == first else first
        assert client.get("/api/media/" + other["id"], headers=headers).status_code == 404
        assert client.post("/api/media/" + other["id"] + "/interpret", headers=headers).status_code == 404
    path = "/api/media/" + first["id"] + "/review"
    assert client.post(path, headers=environment["reviewer_two"]["headers"],
                       json=review_body(first)).status_code == 404
    for actor in ("admin_one", "editor_one", "doctor_one"):
        assert client.post(path, headers=environment[actor]["headers"],
                           json=review_body(first)).status_code == 403
    role_change(environment["editor_one"], "reviewer")
    assert client.post(path, headers=environment["editor_one"]["headers"],
                       json=review_body(first)).status_code == 403
    assert client.get("/api/media/" + first["id"], headers=environment["editor_one"]["headers"]).json()["version"] == 1


def test_media_rejected_text_cannot_be_read_or_accept_review_prose(client, environment, public_source):
    draft = created(client, "/api/media", environment["editor_one"], media_body(environment, public_source))
    path = "/api/media/" + draft["id"]
    before = counts()
    assert client.post(path + "/review", headers=environment["reviewer_one"]["headers"],
                       json=review_body(draft, comment="private notes")).status_code == 422
    assert counts() == before
    rejected = client.post(path + "/review", headers=environment["reviewer_one"]["headers"],
                           json=review_body(draft, decision="rejected"))
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert client.post(path + "/interpret", headers=environment["editor_one"]["headers"]).status_code == 409


@pytest.mark.parametrize("prefix,actor", [("/api/clinical", "editor_one"), ("/api/media", "editor_one")])
@pytest.mark.parametrize("query", ["limit=0", "limit=51", "offset=-1", "offset=2001", "status=paid"])
def test_list_bounds(client, environment, prefix, actor, query):
    assert client.get(prefix + "?" + query, headers=environment[actor]["headers"]).status_code == 422


def test_membership_default_disabled(client, environment):
    before = counts()
    member = environment["member_one"]
    assert client.post("/api/membership/orders", headers=member["headers"], json={}).status_code == 503
    assert client.post("/api/membership/webhook", json={}).status_code == 503
    info = client.get("/api/membership/me", headers=member["headers"]).json()
    assert info["payment_mode"] == "disabled" and info["sandbox_credits"] == 0
    assert info["real_membership"] is False
    assert counts() == before


@pytest.mark.parametrize("mode", ["disabled", "live", "production", "", "SANDBOX"])
def test_unsupported_payment_modes_fail_closed(client, environment, monkeypatch, mode):
    monkeypatch.setenv("APP_PAYMENT_MODE", mode)
    before = counts()
    assert client.post("/api/membership/orders", headers=environment["member_one"]["headers"], json={}).status_code == 503
    assert client.post("/api/membership/webhook", json={}).status_code == 503
    assert counts() == before


@pytest.mark.parametrize("extra", [
    {"amount_fen": 1}, {"amount_fen": 100.0}, {"currency": "USD"}, {"paid": True},
    {"status": "paid"}, {"role": "admin"}, {"membership": True}, {"credits": 999},
    {"owner": "someone-else"}, {"card_number": "not-a-real-card"},
    {"payment_token": "untrusted"}, {"product": "live-membership"},
])
def test_member_body_cannot_set_price_paid_or_entitlements(sandbox, environment, extra):
    before = counts()
    assert sandbox.post("/api/membership/orders", headers=environment["member_one"]["headers"],
                        json=extra).status_code == 422
    assert counts() == before
    assert sandbox.post("/api/membership/me", headers=environment["member_one"]["headers"],
                        json={"credits": 999, "role": "admin"}).status_code == 405


def test_order_ownership_price_and_current_role(sandbox, environment):
    owner = environment["member_one"]
    order = created(sandbox, "/api/membership/orders", owner, {})
    assert (order["status"], order["version"], order["owner"]) == ("pending", 1, owner["id"])
    assert type(order["payload"]["amount_fen"]) is int and order["payload"]["amount_fen"] == 100
    assert order["payload"]["currency"] == "CNY" and order["payload"]["mode"] == "sandbox"
    path = "/api/membership/orders/" + order["id"]
    assert sandbox.get(path, headers=owner["headers"]).json() == order
    assert sandbox.get(path, headers=environment["member_two"]["headers"]).status_code == 404
    assert sandbox.get(path, headers=environment["admin_one"]["headers"]).status_code == 403
    with transaction() as db:
        assert get(db, "membership_order", order["id"]) == order
        assert db.execute("SELECT role FROM users WHERE id=?", (owner["id"],)).fetchone()[0] == "member"


def test_sandbox_atomic_persistence_and_identical_replay(sandbox, environment):
    owner = environment["member_one"]
    order = created(sandbox, "/api/membership/orders", owner, {})
    event = event_body(order)
    response = callback(sandbox, event)
    assert response.status_code == 200, response.text
    assert response.json()["idempotent"] is False and response.json()["real_payment"] is False
    after = counts()
    repeated = callback(sandbox, event)
    assert repeated.status_code == 200 and repeated.json()["idempotent"] is True
    assert counts() == after
    with transaction() as db:
        stored = get(db, "membership_order", order["id"])
        assert stored["status"] == "paid" and stored["version"] == 2
        assert stored["payload"]["paid_event_id"] == event["event_id"]
        credits = listing(db, "membership_entitlement", owner=owner["id"])
        assert len(credits) == 1 and credits[0]["payload"]["credits"] == 1
        assert db.execute("SELECT digest FROM payment_events WHERE id=?", (event["event_id"],)).fetchone()[0] == hashlib.sha256(encode_event(event)).hexdigest()
        assert db.execute("SELECT role FROM users WHERE id=?", (owner["id"],)).fetchone()[0] == "member"
    with browser_client(sandbox.app) as restarted:
        info = restarted.get("/api/membership/me", headers=owner["headers"]).json()
        assert info["sandbox_credits"] == 1 and info["real_membership"] is False
        assert restarted.get("/api/membership/me", headers=environment["member_two"]["headers"]).json()["sandbox_credits"] == 0


def test_each_new_order_adds_one_sandbox_credit(sandbox, environment):
    owner = environment["member_one"]
    for index in (1, 2):
        order = created(sandbox, "/api/membership/orders", owner, {})
        assert callback(sandbox, event_body(order)).status_code == 200
        info = sandbox.get("/api/membership/me", headers=owner["headers"]).json()
        assert (info["sandbox_credits"], info["entitlement_version"]) == (index, index)


@pytest.mark.parametrize("secret", ["", "x" * 31])
def test_callback_needs_sufficient_secret(sandbox, monkeypatch, secret):
    monkeypatch.setenv("APP_PAYMENT_SECRET", secret)
    before = counts()
    assert sandbox.post("/api/membership/webhook", json={}).status_code == 503
    assert counts() == before


def test_unsigned_tampered_and_raw_byte_signatures(sandbox, environment):
    order = created(sandbox, "/api/membership/orders", environment["member_one"], {})
    event = event_body(order)
    body = encode_event(event)
    before = counts()
    assert sandbox.post("/api/membership/webhook", content=body,
                        headers={"Content-Type": "application/json"}).status_code == 401
    assert sandbox.post("/api/membership/webhook", content=body,
                        headers=signed_headers(body, "wrong-test-only-secret")).status_code == 401
    altered = encode_event({**event, "amount_fen": 1})
    assert sandbox.post("/api/membership/webhook", content=altered,
                        headers=signed_headers(body)).status_code == 401
    # Signing an equivalent JSON object is not signing the exact transmitted bytes.
    assert sandbox.post("/api/membership/webhook", content=body + b" ",
                        headers=signed_headers(body)).status_code == 401
    assert counts() == before
    assert sandbox.post("/api/membership/webhook", content=body + b" ",
                        headers=signed_headers(body + b" ")).status_code == 200
    after = counts()
    # Same event with different bytes/digest is a conflict even if JSON is equivalent.
    assert callback(sandbox, event).status_code == 409
    assert counts() == after


@pytest.mark.parametrize("age", [-301, 301])
def test_timestamp_window(sandbox, environment, age):
    order = created(sandbox, "/api/membership/orders", environment["member_one"], {})
    before = counts()
    assert callback(sandbox, event_body(order, timestamp=int(time.time()) + age)).status_code == 401
    assert counts() == before


@pytest.mark.parametrize("changes,expected", [
    ({"amount_fen": 99}, 409), ({"currency": "USD"}, 422), ({"amount_fen": "100"}, 422),
    ({"amount_fen": 100.0}, 422), ({"amount_fen": True}, 422), ({"order_id": "f" * 32}, 404),
    ({"event_id": "../../event"}, 422), ({"event_id": "evt_"}, 422),
    ({"event_id": "evt_" + "x" * 65}, 422), ({"event_id": "evt_张三"}, 422),
    ({"event_id": "evt_valid\n"}, 422), ({"event_id": " evt_valid"}, 422),
    ({"timestamp": "123"}, 422), ({"event_type": "payment.refund"}, 422),
    ({"member_id": "forged-member"}, 422), ({"credits": 999}, 422), ({"paid": True}, 422),
])
def test_signed_mismatch_and_untrusted_fields_never_write(sandbox, environment, changes, expected):
    order = created(sandbox, "/api/membership/orders", environment["member_one"], {})
    before = counts()
    response = callback(sandbox, event_body(order, **changes))
    assert response.status_code == expected, response.text
    assert counts() == before
    with transaction() as db:
        assert get(db, "membership_order", order["id"])["status"] == "pending"
        assert listing(db, "membership_entitlement", owner=order["owner"]) == []


def test_event_replay_and_paid_order_cannot_double_grant(sandbox, environment):
    order = created(sandbox, "/api/membership/orders", environment["member_one"], {})
    original = event_body(order)
    assert callback(sandbox, original).status_code == 200
    after = counts()
    assert callback(sandbox, {**original, "amount_fen": 101}).status_code == 409
    assert callback(sandbox, {**original, "event_id": "evt_new_attempt"}).status_code == 409
    assert counts() == after
    assert sandbox.get("/api/membership/me", headers=environment["member_one"]["headers"]).json()["sandbox_credits"] == 1
    other = created(sandbox, "/api/membership/orders", environment["member_two"], {})
    before = counts()
    assert callback(sandbox, {**original, "order_id": other["id"]}).status_code == 409
    assert counts() == before
    with transaction() as db:
        assert get(db, "membership_order", other["id"])["status"] == "pending"


def test_failed_amount_event_does_not_consume_id(sandbox, environment):
    order = created(sandbox, "/api/membership/orders", environment["member_one"], {})
    event = event_body(order, amount_fen=1)
    assert callback(sandbox, event).status_code == 409
    assert callback(sandbox, {**event, "amount_fen": 100}).status_code == 200


def test_callback_disabled_member_does_not_grant(sandbox, environment):
    order = created(sandbox, "/api/membership/orders", environment["member_one"], {})
    with transaction() as db:
        db.execute("UPDATE users SET enabled=0 WHERE id=?", (order["owner"],))
    before = counts()
    assert callback(sandbox, event_body(order)).status_code == 409
    assert counts() == before


@pytest.mark.parametrize("body", [b"{", b"[]", b'{"event_id":"evt_a","event_id":"evt_b"}',
                                  b'{"amount_fen":NaN}', b"\xff"])
def test_signed_bad_json_is_rejected_without_storage(sandbox, body):
    before = counts()
    assert sandbox.post("/api/membership/webhook", content=body, headers=signed_headers(body)).status_code == 422
    assert counts() == before


def test_webhook_bounded_body_and_content_type(sandbox):
    before = counts()
    body = b"x" * 4097
    assert sandbox.post("/api/membership/webhook", content=body,
                        headers=signed_headers(body)).status_code == 413
    headers = signed_headers(b"{}")
    headers["Content-Type"] = "application/octet-stream"
    assert sandbox.post("/api/membership/webhook", content=b"{}", headers=headers).status_code == 415
    assert counts() == before


def test_concurrent_identical_callbacks_are_atomic_and_idempotent(sandbox, environment):
    order = created(sandbox, "/api/membership/orders", environment["member_one"], {})
    event = event_body(order)
    barrier = Barrier(4)

    def worker(_):
        # Independent clients exercise independent DB connections against one file.
        with browser_client(sandbox.app) as instance:
            barrier.wait(timeout=10)
            response = callback(instance, event)
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(4)))
    assert all(status == 200 for status, _ in results), results
    assert sum(not body["idempotent"] for _, body in results) == 1
    with transaction() as db:
        assert db.execute("SELECT count(*) FROM payment_events WHERE id=?", (event["event_id"],)).fetchone()[0] == 1
        assert get(db, "membership_order", order["id"])["version"] == 2
        items = listing(db, "membership_entitlement", owner=order["owner"])
        assert len(items) == 1 and items[0]["payload"]["credits"] == 1
        assert db.execute("SELECT count(*) FROM events WHERE action='payment_event.accepted' AND object_id=?",
                          (event["event_id"],)).fetchone()[0] == 1


def test_concurrent_distinct_events_for_one_order_grant_once(sandbox, environment):
    order = created(sandbox, "/api/membership/orders", environment["member_one"], {})
    events = [event_body(order), event_body(order)]
    barrier = Barrier(2)

    def worker(event):
        with browser_client(sandbox.app) as instance:
            barrier.wait(timeout=10)
            return callback(instance, event).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(worker, events))
    assert sorted(statuses) == [200, 409]
    with transaction() as db:
        assert db.execute("SELECT count(*) FROM payment_events").fetchone()[0] == 1
        assert listing(db, "membership_entitlement", owner=order["owner"])[0]["payload"]["credits"] == 1


def test_grant_failure_rolls_back_event_order_and_audit(sandbox, environment, monkeypatch):
    module = importlib.import_module("membership.main")
    order = created(sandbox, "/api/membership/orders", environment["member_one"], {})
    original_create = module.create

    def fail_entitlement(db, kind, owner, payload, status="draft"):
        if kind == module.ENTITLEMENT_KIND:
            raise RuntimeError("test-only injected local persistence failure")
        return original_create(db, kind, owner, payload, status=status)

    monkeypatch.setattr(module, "create", fail_entitlement)
    event = event_body(order)
    before = counts()
    with pytest.raises(RuntimeError, match="test-only injected"):
        callback(sandbox, event)
    assert counts() == before
    with transaction() as db:
        assert get(db, "membership_order", order["id"])["status"] == "pending"
    monkeypatch.setattr(module, "create", original_create)
    assert callback(sandbox, event).status_code == 200


def test_disabling_sandbox_blocks_callbacks_without_erasing_history(sandbox, environment, monkeypatch):
    member = environment["member_one"]
    order = created(sandbox, "/api/membership/orders", member, {})
    event = event_body(order)
    assert callback(sandbox, event).status_code == 200
    monkeypatch.setenv("APP_PAYMENT_MODE", "disabled")
    before = counts()
    assert callback(sandbox, event).status_code == 503
    assert sandbox.post("/api/membership/orders", headers=member["headers"], json={}).status_code == 503
    info = sandbox.get("/api/membership/me", headers=member["headers"]).json()
    assert info["sandbox_credits"] == 1 and info["sandbox_enabled"] is False
    assert info["real_membership"] is False
    assert sandbox.get("/api/membership/orders/" + order["id"], headers=member["headers"]).json()["status"] == "paid"
    assert counts() == before
