"""Public classical QA only; run this independent entry with one worker.

No input/output logging, credentials, retries, or extraction fallback. The
inference thread owns its admission slot until it actually finishes, even when
its HTTP caller disconnects or cancels the awaiting coroutine.
"""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import ipaddress
import os
from pathlib import Path
import re
from threading import Lock
import time
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send

from common.catalog import DISCLAIMER, trusted
from common.qa import (
    JsonRejected, MODEL_NAME, NOTE_KIND, READING_NOTES, SCOPE, QaResult,
    Question, explanation_ok, question_allowed, retrieve_qa, strict_json,
)
from common.schemas import SourceType, Zone
from public_qa.model import infer as _infer  # Offline HTTP tests patch main._infer only.

MAX_BODY_BYTES = 2048
RATE_PER_MINUTE = 6
PAGE = Path(__file__).with_name("index.html")


def _parse_origin(value: str) -> tuple[str, str, int | None]:
    """Accept exactly one canonical ASCII HTTP(S) origin, never host patterns."""
    try:
        if not value or value != value.strip() or not value.isascii():
            raise ValueError
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in {"http", "https"} or not host or parsed.path
                or parsed.query or parsed.fragment or parsed.username is not None
                or parsed.password is not None or any(c in value for c in "*?#\\")):
            raise ValueError
        if ":" in host:
            host_part = "[" + ipaddress.IPv6Address(host).compressed + "]"
        else:
            if len(host) > 253 or not all(re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label
            ) for label in host.split(".")):
                raise ValueError
            host_part = host
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        authority = host_part + (":" + str(port) if port is not None else "")
        origin = parsed.scheme + "://" + authority
        if origin != value:
            raise ValueError
        return origin, authority, port
    except (TypeError, ValueError):
        raise ValueError("QA_ORIGIN must be one explicit canonical HTTP(S) origin") from None


ORIGIN, ALLOWED_HOST, _PORT = _parse_origin(
    os.environ.get("QA_ORIGIN", "http://127.0.0.1:18101")
)
_PAGE_HTML = PAGE.read_text(encoding="utf-8")
_SCRIPT_HASHES = [
    "'sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii") + "'"
    for script in re.findall(r"<script>(.*?)</script>", _PAGE_HTML, re.S)
]
CSP = (
    "default-src 'none'; script-src " + " ".join(_SCRIPT_HASHES)
    + "; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class PublicBoundary:
    """Pure ASGI boundary: measure actual body chunks before JSON parsing.

    Do not use request-body model validation: its standard errors echo input.
    Content-Length is only an additional consistency check, never the limit.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {key.lower().encode("ascii") for key in _SECURITY_HEADERS}
                headers = [(key, value) for key, value in headers if key.lower() not in names]
                headers.extend((key.lower().encode("ascii"), value.encode("ascii"))
                               for key, value in _SECURITY_HEADERS.items())
                message = {**message, "headers": headers}
            await send(message)

        async def reject(code: int, detail: str):
            await JSONResponse({"detail": detail}, status_code=code)(scope, receive, secure_send)

        headers: dict[bytes, list[bytes]] = {}
        for key, value in scope.get("headers", []):
            headers.setdefault(key.lower(), []).append(value)
        hosts = headers.get(b"host", [])
        if len(hosts) != 1 or hosts[0].lower() != ALLOWED_HOST.encode("ascii"):
            await reject(400, "请求主机不符合入口配置")
            return
        if scope["method"] != "POST" or scope["path"] != "/api/ask":
            await self.app(scope, receive, secure_send)
            return
        if headers.get(b"origin") != [ORIGIN.encode("ascii")]:
            await reject(403, "只接受本站同源问答请求")
            return
        content_types = headers.get(b"content-type", [])
        if (len(content_types) != 1 or not re.fullmatch(
                rb'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?\s*',
                content_types[0].lower().strip())):
            await reject(415, "仅接受 UTF-8 JSON 请求")
            return
        if headers.get(b"content-encoding", [b"identity"]) != [b"identity"]:
            await reject(415, "不接受压缩请求体")
            return
        lengths = headers.get(b"content-length", [])
        transfer = headers.get(b"transfer-encoding", [])
        if (len(lengths) > 1 or (lengths and transfer)
                or (transfer and transfer != [b"chunked"])
                or (lengths and not re.fullmatch(rb"[0-9]{1,10}", lengths[0]))):
            await reject(400, "请求长度或传输格式无效")
            return
        declared = int(lengths[0]) if lengths else None
        if declared is not None and declared > MAX_BODY_BYTES:
            await reject(413, "请求体不得超过 2048 字节")
            return
        body = bytearray()
        while True:
            try:
                message = await receive()
            except ClientDisconnect:
                return
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                await reject(400, "请求体传输无效")
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                await reject(413, "请求体不得超过 2048 字节")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        if declared is not None and declared != len(body):
            await reject(400, "请求长度与实际请求体不符")
            return
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, secure_send)


_lock = Lock()
_calls: deque[float] = deque()
_active = 0
_clock = time.monotonic


def _verify(raw: QaResult, question: str, task_id: str) -> QaResult:
    """Independently bind the engine response to this task and trusted retrieval."""
    if not isinstance(raw, QaResult):
        raise ValueError("invalid result")
    # Serializer warnings can contain malformed field values; never emit them.
    data = raw.model_dump(mode="python", warnings=False)
    if set(data) != {"task_id", "question", "explanation", "sources", "model", "check", "generation", "disclaimer"}:
        raise ValueError("incomplete result")
    if (not isinstance(data["check"], dict) or set(data["check"]) != {"citations_ok", "format_ok", "scope"}
            or not isinstance(data["generation"], dict)
            or set(data["generation"]) != {"prompt_tokens", "output_tokens"}
            or not isinstance(data["sources"], list) or not 1 <= len(data["sources"]) <= 2):
        raise ValueError("invalid result fields")
    for source in data["sources"]:
        if (not isinstance(source, dict) or set(source) != {"provenance", "reading_note", "note_kind"}
                or not isinstance(source["provenance"], dict)
                or set(source["provenance"]) != {"source_id", "title", "locator", "source_type", "quote", "version", "content_hash"}):
            raise ValueError("incomplete provenance")
    result = QaResult.model_validate(data, strict=True)
    if (result.task_id != task_id or result.question != question
            or result.model != "ollama:" + MODEL_NAME
            or result.check.citations_ok is not True or result.check.format_ok is not True
            or result.check.scope != SCOPE or result.disclaimer != DISCLAIMER
            or type(data["generation"]["prompt_tokens"]) is not int
            or type(data["generation"]["output_tokens"]) is not int
            or not 0 < data["generation"]["prompt_tokens"] <= 768
            or not 0 < data["generation"]["output_tokens"] <= 256):
        raise ValueError("unverified result")
    evidence = retrieve_qa(question)
    allowed = {item.provenance.source_id: item for item in evidence}
    if len(allowed) != len(evidence):
        raise ValueError("ambiguous retrieval")
    selected = []
    seen = set()
    for source in result.sources:
        sid = source.provenance.source_id
        ref = allowed.get(sid)
        if (ref is None or sid in seen or ref.zone != Zone.GREEN
                or ref.provenance.source_type != SourceType.PUBLIC_CLASSIC or not trusted(ref)
                or source.provenance.model_dump(mode="json") != ref.provenance.model_dump(mode="json")
                or source.reading_note != READING_NOTES.get(sid) or source.note_kind != NOTE_KIND):
            raise ValueError("untrusted source")
        seen.add(sid)
        selected.append(ref)
    if not selected or not explanation_ok(result.explanation, selected):
        raise ValueError("unverified explanation")
    return result


def _answer(question: str) -> JSONResponse:
    """Keep acquisition AND finally in the worker thread, not its HTTP awaiter."""
    global _active
    with _lock:
        now = _clock()
        while _calls and now - _calls[0] >= 60:
            _calls.popleft()
        if len(_calls) >= RATE_PER_MINUTE or _active >= 1:
            return JSONResponse({"detail": "已有问答运行或达到每分钟限制"}, status_code=429,
                                headers={"Retry-After": "60"})
        _calls.append(now)  # Admitted failed inference also consumes the minute budget.
        _active += 1
    try:
        task_id = "publicqa-" + uuid.uuid4().hex
        result = _verify(_infer(question, task_id), question, task_id)
        return JSONResponse(result.model_dump(mode="json"))
    except Exception:
        # Never expose exception strings, validation input, partial output, or a fallback.
        return JSONResponse({"detail": "推理或入口核验失败，未发布结果"}, status_code=503)
    finally:
        with _lock:
            _active -= 1


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(PublicBoundary)


@app.get("/")
def page():
    return HTMLResponse(_PAGE_HTML)


@app.get("/health")
def health():
    # This is HTTP liveness, not evidence that a real model question has passed.
    return {"status": "ok", "module": "public_qa", "worker_policy": "single", "scope": "public_classics_only"}


@app.post("/api/ask")
async def ask(request: Request):
    try:
        payload = Question.model_validate(strict_json(await request.body()), strict=True)
        if not question_allowed(payload.question):
            raise JsonRejected("invalid question scope")
    except (JsonRejected, ValidationError, ValueError, TypeError, RecursionError):
        return JSONResponse({"detail": "仅接受 2 到 300 字的公开经典文化问题，禁止私人或诊疗输入"}, status_code=422)
    return await run_in_threadpool(_answer, payload.question)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=_PORT or (443 if ORIGIN.startswith("https:") else 80),
                workers=1, access_log=False, proxy_headers=False)
