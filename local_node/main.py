"""Replaceable private capability node; only exact public classic IDs cross it.

This is a Linux-container role simulator, not proof of Windows deployment. Red
corpora, clinical records, local reasoning and credentials are never returned.
"""
import hmac
import os
from pathlib import Path
import re
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field
from application_hub.security import Input
from common.catalog import catalog
from common.qa import strict_json
from common.schemas import SourceType, Zone


class NodeBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                await JSONResponse({"detail": "节点请求无效"}, status_code=400)(scope, receive, send)
                return
            body.extend(message.get("body", b""))
            if len(body) > 1024:
                await JSONResponse({"detail": "节点请求过大"}, status_code=413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        try:
            strict_json(bytes(body))
        except (ValueError, TypeError, RecursionError):
            await JSONResponse({"detail": "节点请求无效"}, status_code=422)(scope, receive, send)
            return
        delivered = False

        async def bounded():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded, send)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(NodeBoundary)


class Lookup(Input):
    source_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_-]+$")


def node_key(x_node_key: str | None = Header(default=None)):
    path = os.environ.get("NODE_SECRET_FILE", "")
    try:
        secret = Path(path).read_text().strip() if path else ""
    except OSError:
        secret = ""
    if len(secret) < 32:
        raise HTTPException(503, "节点鉴权未配置")
    if not x_node_key or not hmac.compare_digest(secret, x_node_key):
        raise HTTPException(401, "节点鉴权失败")


@app.exception_handler(RequestValidationError)
async def invalid(_request, _exception):
    return JSONResponse({"detail": "节点请求无效"}, status_code=422)


@app.get("/health")
def health():
    # Does not expose keys, machine URLs, corpus contents or OS claims.
    return {"status": "ok", "capability": "public_classic_lookup", "role_simulation": True}


@app.post("/v1/public/source", dependencies=[Depends(node_key)])
def lookup(req: Lookup):
    node = os.environ.get("NODE_ID", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", node):
        raise HTTPException(503, "节点身份未配置")
    found = next((e for e in catalog(Zone.GREEN) if e.provenance.source_id == req.source_id
                  and e.provenance.source_type == SourceType.PUBLIC_CLASSIC), None)
    if found is None:
        raise HTTPException(404, "不在公开经典范围")
    return {"node_id": node, "evidence": found.model_dump(mode="json"),
            "scope": "public_classics_only", "role_simulation": True}
