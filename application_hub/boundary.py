"""Actual byte and origin boundary shared by portable browser-facing services."""
import os
import re
from fastapi.responses import JSONResponse
from public_qa.main import _parse_origin


class BrowserBoundary:
    def __init__(self, app):
        self.app = app
        self.origin, self.host, _ = _parse_origin(os.environ.get("APP_ORIGIN", "http://127.0.0.1:18121"))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                            (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY")]
                message = {**message, "headers": headers}
            await send(message)

        async def reject(code, text):
            await JSONResponse({"detail": text}, status_code=code)(scope, receive, secure)

        headers = {}
        for key, value in scope["headers"]:
            headers.setdefault(key.lower(), []).append(value)
        if headers.get(b"host") != [self.host.encode()]:
            await reject(400, "主机不符合配置")
            return
        if len(headers.get(b"authorization", [])) > 1:
            await reject(400, "鉴权头不能重复")
            return
        if scope["method"] in {"GET", "HEAD"}:
            # Never buffer a GET body in the downstream proxy.
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request" or message.get("body") or message.get("more_body", False):
                await reject(400, "读取请求不能包含正文")
                return
            upstream_receive = receive
            delivered = False

            async def empty_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return await upstream_receive()

            receive = empty_receive
        if scope["method"] not in {"GET", "HEAD"}:
            # Sandbox webhook has HMAC boundary rather than a browser Origin.
            webhook = scope["path"] == "/api/membership/webhook"
            if not webhook and headers.get(b"origin") != [self.origin.encode()]:
                await reject(403, "仅接受本站请求")
                return
            if headers.get(b"content-encoding", [b"identity"]) != [b"identity"]:
                await reject(415, "不接受压缩输入")
                return
            content_type = headers.get(b"content-type", [])
            if content_type and (len(content_type) != 1 or not re.fullmatch(rb"application/json(?:;\s*charset=utf-8)?", content_type[0].lower())):
                await reject(415, "只接受JSON输入")
                return
            lengths, transfers = headers.get(b"content-length", []), headers.get(b"transfer-encoding", [])
            if (len(lengths) > 1 or (lengths and transfers) or
                    (transfers and transfers != [b"chunked"]) or
                    (lengths and not re.fullmatch(rb"[0-9]{1,8}", lengths[0]))):
                await reject(400, "输入长度格式无效")
                return
            bound = 4096 if webhook else (2048 if scope["path"] == "/api/ask" else 32768)
            declared = int(lengths[0]) if lengths else None
            if declared is not None and declared > bound:
                await reject(413, "输入过大")
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    await reject(400, "输入传输无效")
                    return
                body.extend(message.get("body", b""))
                if len(body) > bound:
                    await reject(413, "输入过大")
                    return
                if not message.get("more_body", False):
                    break
            if declared is not None and len(body) != declared:
                await reject(400, "输入长度不符")
                return
            # Bodyless actions are valid; nonempty bodies must be strict JSON.
            if body and not content_type:
                await reject(415, "只接受JSON输入")
                return
            from common.qa import strict_json
            try:
                if body:
                    strict_json(bytes(body))
            except (ValueError, TypeError, RecursionError):
                await reject(422, "JSON输入无效")
                return
            delivered = False
            upstream_receive = receive

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await upstream_receive()

            receive = bounded_receive
        await self.app(scope, receive, secure)
