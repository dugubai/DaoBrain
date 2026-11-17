"""Six-module persistent application prototype, separate from legacy simulation.

One worker; no bootstrap on import, no payment live mode, no real medical input.
The QA handler and verifier are reused unchanged, including failure closure.
"""
import sqlite3
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from application_hub.boundary import BrowserBoundary
from application_hub.security import Login, NewUser, add_user, current, digest_token, login, roles
from application_hub.store import audit, transaction
from application_hub.nodes import router as nodes
from card_workflow.main import router as cards
from knowledge_library.main import router as library
from clinical_records.main import router as clinical
from media_review.main import router as media
from membership.main import router as membership
from public_qa.main import ask

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(BrowserBoundary)
for router in (cards, library, clinical, media, membership, nodes):
    app.include_router(router)


@app.exception_handler(RequestValidationError)
async def bad_input(_request, _error):
    return JSONResponse({"detail": "输入字段或类型无效"}, status_code=422)


@app.exception_handler(sqlite3.Error)
async def storage_error(_request, _error):
    return JSONResponse({"detail": "业务存储暂不可用"}, status_code=503)


@app.get("/health")
def health():
    with transaction() as db:
        db.execute("SELECT 1")
    return {"status": "ok", "module": "application_hub", "stage": "business_prototype",
            "persistence": "single_host_sqlite", "windows_verified": False,
            "semantic_qa_accepted": False, "live_payment": False}


@app.get("/api/capabilities")
def capabilities():
    return {"cards": "persistent_review_canonical_public_only", "library": "reviewed_lexical_prototype",
            "qa": "real_ollama_public_classics_quality_pending", "clinical": "synthetic_records_only",
            "media": "approved_text_only_visual_unavailable", "membership": "optional_sandbox_only",
            "local_nodes": "public_classic_role_simulation", "production_accepted": False}


@app.post("/api/auth/login")
def sign_in(req: Login):
    return login(req)


@app.get("/api/auth/me")
def who(user=Depends(current)):
    return {"id": user.id, "name": user.name, "role": user.role}


@app.post("/api/auth/logout")
def sign_out(user=Depends(current), authorization: str = Header()):
    with transaction() as db:
        db.execute("UPDATE sessions SET expires=0 WHERE digest=?", (digest_token(authorization[7:]),))
        audit(db, user.id, "session.logout", user.id)
    return {"logged_out": True}


@app.post("/api/auth/users", status_code=201)
def create_user(req: NewUser, user=Depends(roles("admin"))):
    with transaction() as db:
        return add_user(db, req, user.id)


@app.post("/api/ask", dependencies=[Depends(current)])
async def question(request: Request):
    return await ask(request)


@app.get("/api/audit", dependencies=[Depends(roles("admin"))])
def events():
    with transaction() as db:
        return {"events": [dict(row) for row in db.execute("SELECT * FROM events ORDER BY created DESC,id DESC LIMIT 100")],
                "contents_logged": False}
