"""公网验收curl通道的离线契约；这些测试不代表公网或模型验收。"""
from types import SimpleNamespace

import httpx
import pytest

from scripts import verify_public_demo as verifier


@pytest.mark.parametrize("status", [200, 302, 403, 422, 404, 503])
def test_curl_is_real_bounded_no_proxy_no_redirect(monkeypatch, status):
    def run(args, **kwargs):
        assert args[:2] == ["curl", "-q"]
        assert "-L" not in args and "--location" not in args
        assert args[args.index("--noproxy") + 1] == "*"
        assert args[args.index("--max-time") + 1] == "125"
        assert args[args.index("--max-filesize") + 1] == "65536"
        assert args[args.index("--proto") + 1] == "=http"
        assert "--data-binary" in args
        assert kwargs == {"input": b'{"sample_id":"seasons"}',
                          "capture_output": True, "timeout": 130}
        return SimpleNamespace(returncode=0, stderr=b"", stdout=(
            f"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\n"
            "Content-Length: 2\r\n\r\n{}".encode()))
    monkeypatch.setattr(verifier.subprocess, "run", run)
    request = httpx.Request("POST", verifier.BASE + "api/demo",
                           headers={"Origin": verifier.ORIGIN,
                                    "Content-Type": "application/json"},
                           content=b'{"sample_id":"seasons"}')
    response = verifier.CurlTransport().handle_request(request)
    assert response.status_code == status
    assert response.json() == {}


@pytest.mark.parametrize("url", [
    "https://39.102.49.1/daobrain/", "http://evil.example/daobrain/",
    "http://39.102.49.1:8000/daobrain/", "http://39.102.49.1/v1/query",
    "http://user:secret@39.102.49.1/daobrain/",
])
def test_curl_rejects_unapproved_target_before_subprocess(monkeypatch, url):
    monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **k: pytest.fail("不应联网"))
    with pytest.raises(RuntimeError, match="已授权"):
        verifier.CurlTransport().handle_request(httpx.Request("GET", url))


@pytest.mark.parametrize("key", ["Authorization", "X-API-Key", "X-Internal-Key", "Cookie"])
def test_curl_rejects_credentials_before_subprocess(monkeypatch, key):
    monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **k: pytest.fail("不应联网"))
    with pytest.raises(RuntimeError, match="凭证"):
        verifier.CurlTransport().handle_request(httpx.Request("GET", verifier.BASE,
                                                              headers={key: "secret"}))


@pytest.mark.parametrize("code,stdout", [
    (7, b""), (28, b""), (0, b"not HTTP"),
    (0, b"HTTP/1.1 200 OK\r\nBadHeader\r\n\r\n{}"),
    (0, b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 65537),
])
def test_curl_error_cannot_become_success(monkeypatch, code, stdout):
    monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=code, stdout=stdout, stderr=b"transport failed"))
    with pytest.raises(RuntimeError):
        verifier.CurlTransport().handle_request(httpx.Request("GET", verifier.BASE))
