"""Same-origin portal proxy. No credentials in its image, no public arbitrary proxy."""
import base64
import hashlib
import os
from pathlib import Path
import re
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from application_hub.boundary import BrowserBoundary
from application_hub.nodes import private_url
from public_qa.main import _parse_origin

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(BrowserBoundary)
PAGE = Path(__file__).with_name("index.html").read_text()
HASHES = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
                  for s in re.findall(r"<script>(.*?)</script>", PAGE, re.S))
CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src " + HASHES + "; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"


@app.get("/")
def index():
    return HTMLResponse(PAGE, headers={"Content-Security-Policy": CSP})


@app.get("/health")
def health():
    return {"status": "ok", "role": "frontend", "backend_checked": False}


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH"])
async def proxy(path: str, request: Request):
    # No arbitrary target, encoded traversal, query-defined backend or forwarding headers.
    if (not re.fullmatch(r"[a-zA-Z0-9_/-]{1,180}", path) or "//" in path
            or path.split("/", 1)[0] not in {"auth", "cards", "library", "clinical", "media", "membership", "ask", "nodes", "capabilities", "audit"}
            or len(request.url.query) > 500):
        return JSONResponse({"detail": "不支持此接口"}, status_code=404)
    try:
        upstream = private_url(os.environ.get("APP_BACKEND_URL", ""))
        origin, host, _ = _parse_origin(os.environ.get("APP_ORIGIN", "http://127.0.0.1:18121"))
        headers = {"Host": host, "Origin": origin, "Content-Type": "application/json"}
        for name in ("authorization", "x-payment-signature", "x-payment-timestamp"):
            values = request.headers.getlist(name)
            if len(values) > 1:
                raise ValueError
            if values:
                headers[name] = values[0]
        body = await request.body()
        if len(body) > 32768:
            raise ValueError
        async with httpx.AsyncClient(timeout=httpx.Timeout(125, connect=5), trust_env=False, follow_redirects=False) as client:
            async with client.stream(request.method, upstream + "/api/" + path,
                                     params=list(request.query_params.multi_items()), headers=headers, content=body) as response:
                if response.is_redirect or response.headers.get("content-type", "").split(";")[0] != "application/json":
                    raise ValueError
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 262144:
                        raise ValueError
                # Never propagate server cookies, stack traces, locations or raw headers.
                return Response(bytes(data), status_code=response.status_code, media_type="application/json")
    except (httpx.HTTPError, ValueError, TypeError):
        return JSONResponse({"detail": "后端未连接或返回无效；未回退"}, status_code=503)
