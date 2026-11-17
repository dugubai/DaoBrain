"""VM-run regression; no installs or success-stub quality claims."""
import asyncio
from contextlib import closing
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from application_hub.boundary import BrowserBoundary
from application_hub.main import app
from application_hub.security import NewUser, _attempts, add_user, digest_token, password_matches
from application_hub.store import transaction

ORIGIN = "http://127.0.0.1:18121"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "business.sqlite3"))
    monkeypatch.setenv("APP_ORIGIN", ORIGIN)
    _attempts.clear()
    with transaction() as db:
        add_user(db, NewUser(name="adminuser", password="Long-test-only-password", role="admin"), "fixture")
        add_user(db, NewUser(name="memberuser", password="Long-test-only-password", role="member"), "fixture")
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as value:
        yield value


def auth(client, name="adminuser"):
    response = client.post("/api/auth/login", json={"name": name, "password": "Long-test-only-password"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_session_hashed_expiry_and_logout(client):
    headers = auth(client)
    assert client.get("/api/auth/me", headers=headers).json()["role"] == "admin"
    with transaction() as db:
        row = db.execute("SELECT digest FROM sessions").fetchone()
        assert row[0] == digest_token(headers["Authorization"][7:])
        assert row[0] != headers["Authorization"][7:]
        assert db.execute("SELECT password FROM users LIMIT 1").fetchone()[0] != "Long-test-only-password"
    assert client.post("/api/auth/logout", json={}, headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_roles_and_identity_not_supplied(client):
    headers = auth(client, "memberuser")
    result = client.post("/api/auth/users", headers=headers,
                         json={"name": "newmember", "password": "Long-test-only-password", "role": "admin"})
    assert result.status_code == 403
    assert client.get("/api/audit", headers=headers).status_code == 403
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + "x" * 43, "X-User": "adminuser"}).status_code == 401


@pytest.mark.parametrize("payload", ['{"name":"adminuser","name":"memberuser"}', '{"x":1e999}', '{"x":NaN}', '[1,2]'])
def test_strict_json_no_echo(client, payload):
    response = client.post("/api/auth/login", content=payload, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert "adminuser" not in response.text and "1e999" not in response.text


def test_host_origin_body_and_generic_validation(client):
    assert client.get("/health", headers={"Host": "attacker.example"}).status_code == 400
    assert client.post("/api/auth/login", json={}, headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.post("/api/auth/login", content=b"x" * 32769,
                       headers={"Content-Type": "application/json"}).status_code == 413
    response = client.post("/api/auth/login", json={"name": "sensitive-input", "password": "short"})
    assert response.status_code == 422 and "sensitive-input" not in response.text


def test_health_never_claims_production(client):
    status = client.get("/health").json()
    assert status["windows_verified"] is False
    assert status["semantic_qa_accepted"] is False
    assert client.get("/api/capabilities").json()["production_accepted"] is False


def test_missing_db_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_DB_PATH", raising=False)
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/health").status_code == 503


def test_node_auth_and_canonical_scope(tmp_path, monkeypatch):
    from local_node.main import app as node
    key = tmp_path / "node.key"
    key.write_text("K" * 48)
    monkeypatch.setenv("NODE_SECRET_FILE", str(key))
    monkeypatch.setenv("NODE_ID", "local-one")
    with TestClient(node) as c:
        assert c.post("/v1/public/source", json={"source_id": "xueji-mutual"}).status_code == 401
        headers = {"X-Node-Key": "K" * 48}
        data = c.post("/v1/public/source", json={"source_id": "xueji-mutual"}, headers=headers).json()
        assert data["scope"] == "public_classics_only"
        assert data["evidence"]["zone"] == "green"
        assert c.post("/v1/public/source", json={"source_id": "private-cases"}, headers=headers).status_code == 404
        assert c.post("/v1/public/source", json={"source_id": "xueji-mutual", "zone": "red"}, headers=headers).status_code == 422


@pytest.mark.parametrize("url", ["http://8.161.228.168:8000", "http://169.254.169.254:8000", "file:///secret", "http://user:secret@backend:8000", "http://backend:8000?next=x", "http://backend:80"])
def test_private_endpoints_not_arbitrary(url):
    from application_hub.nodes import private_url
    with pytest.raises(ValueError):
        private_url(url)


def test_fixed_local_endpoints():
    from application_hub.nodes import private_url
    assert private_url("http://backend:8000") == "http://backend:8000"
    assert private_url("http://127.0.0.1:18131") == "http://127.0.0.1:18131"


def test_existing_version_two_refuses_without_schema_data_or_byte_changes(tmp_path, monkeypatch):
    source = tmp_path / "future.sqlite3"
    monkeypatch.setenv("APP_DB_PATH", str(source))
    monkeypatch.setenv("APP_ORIGIN", ORIGIN)
    monkeypatch.setattr(app, "middleware_stack", None)
    with closing(sqlite3.connect(str(source))) as db:
        db.executescript("""
            CREATE TABLE schema_version(version INTEGER PRIMARY KEY);
            INSERT INTO schema_version VALUES(2);
            CREATE TABLE future_records(id INTEGER PRIMARY KEY, note TEXT NOT NULL);
            INSERT INTO future_records VALUES(1, 'synthetic-version-two-marker');
            CREATE INDEX future_note ON future_records(note);
            CREATE VIEW future_view AS SELECT note FROM future_records;
            CREATE TRIGGER future_guard BEFORE DELETE ON future_records
                BEGIN SELECT RAISE(ABORT, 'preserve future data'); END;
        """)

    def snapshot():
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
            return (db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
                    db.execute("PRAGMA table_info(future_records)").fetchall(),
                    tuple(db.iterdump()))

    before_bytes, before_database = source.read_bytes(), snapshot()
    with pytest.raises(HTTPException) as error:
        with transaction():
            pytest.fail("an incompatible database must never enter the transaction body")
    assert error.value.status_code == 503
    assert source.read_bytes() == before_bytes
    assert snapshot() == before_database
    with TestClient(app, base_url=ORIGIN) as value:
        response = value.get("/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "数据库版本不匹配"}
    assert "synthetic-version-two-marker" not in response.text
    assert snapshot() == before_database
    assert source.read_bytes() == before_bytes


@pytest.fixture
def browser_apps(client, monkeypatch):
    from portable_frontend import main as frontend
    monkeypatch.setenv("APP_BACKEND_URL", "http://backend:8000")
    # Rebuild the real middleware with this test's origin, irrespective of order.
    for service in (app, frontend.app):
        assert any(item.cls is BrowserBoundary for item in service.user_middleware)
        monkeypatch.setattr(service, "middleware_stack", None)
    real_client = httpx.AsyncClient

    def in_process_client(*args, **kwargs):
        # Only replace the transport: responses, auth, SQL and proxying stay real.
        assert "transport" not in kwargs
        kwargs["transport"] = httpx.ASGITransport(app=app)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(frontend.httpx, "AsyncClient", in_process_client)
    return {"backend": app, "frontend": frontend.app}


async def browser_exchange(service, method, path, messages, extra_headers=()):
    """Deliver exact ASGI frames; an HTTP client may coalesce empty chunks."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": [(b"host", b"127.0.0.1:18121"),
             (b"origin", ORIGIN.encode()), *extra_headers],
             "server": ("127.0.0.1", 18121), "client": ("127.0.0.1", 12345)}
    sent, received = [], []

    async def receive():
        index = len(received)
        received.append(index)
        if index < len(messages):
            return dict(messages[index])
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await service(scope, receive, send)
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert len(starts) == 1
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return starts[0]["status"], body, dict(starts[0]["headers"]), received


@pytest.mark.parametrize("service", ["backend", "frontend"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("body,more_body,framing", [
    pytest.param(b"synthetic-body-marker", False, (), id="body-without-length"),
    pytest.param(b"\x00", False, ((b"content-length", b"0"),), id="body-with-zero-length"),
    pytest.param(b"synthetic-body-marker", True, (), id="nonempty-stream"),
    pytest.param(b"", True, (), id="empty-more-body-without-length"),
    pytest.param(b"", True, ((b"transfer-encoding", b"chunked"),), id="empty-chunked-more-body"),
])
def test_get_head_body_rejected_before_auth_proxy_or_second_frame(
        browser_apps, tmp_path, monkeypatch, service, method, body, more_body, framing):
    def forbidden_client(*_args, **_kwargs):
        pytest.fail("a rejected GET/HEAD must not enter the frontend proxy")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    source = tmp_path / "business.sqlite3"
    before = source.read_bytes()
    status, response, headers, received = asyncio.run(browser_exchange(
        browser_apps[service], method, "/api/auth/me", [
            {"type": "http.request", "body": body, "more_body": more_body},
            {"type": "http.request", "body": b"must-not-be-consumed", "more_body": False},
        ], framing))
    assert status == 400
    assert json.loads(response) == {"detail": "读取请求不能包含正文"}
    assert b"synthetic-body-marker" not in response and b"must-not-be-consumed" not in response
    assert received == [0]
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert source.read_bytes() == before


@pytest.mark.parametrize("service", ["backend", "frontend"])
@pytest.mark.parametrize("method,status", [("GET", 200), ("HEAD", 405)])
@pytest.mark.parametrize("message", [
    {"type": "http.request", "body": b"", "more_body": False},
    {"type": "http.request"},
])
def test_empty_get_head_reaches_real_router(browser_apps, monkeypatch, service, method, status, message):
    def forbidden_client(*_args, **_kwargs):
        pytest.fail("health must not use a network client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    code, response, _, _ = asyncio.run(browser_exchange(
        browser_apps[service], method, "/health", [message]))
    # FastAPI defines GET only here: HEAD's genuine router result is 405, not 400.
    assert code == status
    if method == "GET":
        assert json.loads(response)["status"] == "ok"


EMPTY_POST_FRAMING = [
    pytest.param((), [{"type": "http.request", "body": b"", "more_body": False}], id="no-type-no-length"),
    pytest.param(((b"content-length", b"0"),), [{"type": "http.request"}], id="zero-length-no-type"),
    pytest.param(((b"transfer-encoding", b"chunked"),), [
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": b"", "more_body": False},
    ], id="empty-chunked-no-type"),
]


@pytest.mark.parametrize("service", ["backend", "frontend"])
@pytest.mark.parametrize("framing,messages", EMPTY_POST_FRAMING)
def test_bodyless_logout_invalidates_real_session(client, browser_apps, service, framing, messages):
    authorization = auth(client)["Authorization"]
    with transaction() as db:
        assert db.execute("SELECT expires FROM sessions WHERE digest=?",
                          (digest_token(authorization[7:]),)).fetchone()[0] > int(time.time())
    status, response, _, received = asyncio.run(browser_exchange(
        browser_apps[service], "POST", "/api/auth/logout", messages,
        (*framing, (b"authorization", authorization.encode()))))
    assert status == 200 and json.loads(response) == {"logged_out": True}
    assert received == list(range(len(messages)))
    with transaction() as db:
        assert db.execute("SELECT expires FROM sessions WHERE digest=?",
                          (digest_token(authorization[7:]),)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events WHERE action='session.logout'").fetchone()[0] == 1
    assert client.get("/api/auth/me", headers={"Authorization": authorization}).status_code == 401


@pytest.mark.parametrize("service", ["backend", "frontend"])
@pytest.mark.parametrize("path,status", [("/api/auth/logout", 401), ("/api/auth/login", 422)])
def test_empty_post_keeps_real_auth_and_validation_errors(browser_apps, service, path, status):
    code, response, _, received = asyncio.run(browser_exchange(
        browser_apps[service], "POST", path, [{"type": "http.request", "body": b"", "more_body": False}]))
    assert code == status
    assert set(json.loads(response)) == {"detail"}
    assert received == [0]


@pytest.mark.parametrize("service", ["backend", "frontend"])
def test_empty_first_post_chunk_does_not_allow_nonempty_body_without_json_type(browser_apps, service):
    status, response, _, received = asyncio.run(browser_exchange(
        browser_apps[service], "POST", "/api/auth/logout", [
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"{}", "more_body": False},
        ], ((b"transfer-encoding", b"chunked"),)))
    assert status == 415 and json.loads(response) == {"detail": "只接受JSON输入"}
    assert received == [0, 1]
