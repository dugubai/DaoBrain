"""Person-bound local sessions. No default account, remote credential or public signup."""
from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
import os
import re
import secrets
from threading import Lock
import time
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from application_hub.store import audit, transaction

ROLES = {"admin", "editor", "reviewer", "clinician", "member"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Review(Input):
    expected_version: int = Field(ge=1, le=10000)
    decision: str = Field(pattern="^(approved|rejected)$")
    comment: str = Field(default="", max_length=1000)


@dataclass(frozen=True)
class Principal:
    id: str
    role: str
    name: str


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 240000).hex()
    return "pbkdf2$240000$" + salt + "$" + digest


def password_matches(password, stored):
    try:
        scheme, rounds, salt, expected = stored.split("$")
        if scheme != "pbkdf2" or rounds != "240000":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 240000).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def digest_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current(authorization: str | None = Header(default=None)):
    if not authorization or not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", authorization):
        raise HTTPException(401, "需要登录")
    with transaction() as db:
        row = db.execute("SELECT u.id,u.role,u.name FROM sessions s JOIN users u ON u.id=s.user_id "
                         "WHERE s.digest=? AND s.expires>? AND u.enabled=1",
                         (digest_token(authorization[7:]), int(time.time()))).fetchone()
        if not row:
            raise HTTPException(401, "登录已失效")
        return Principal(**dict(row))


def roles(*allowed):
    def dependency(user: Principal = Depends(current)):
        if user.role not in allowed:
            raise HTTPException(403, "当前账号无此权限")
        return user
    return dependency


class Login(Input):
    name: str = Field(min_length=3, max_length=40, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]+$")
    password: str = Field(min_length=12, max_length=128)


class NewUser(Login):
    role: str = Field(pattern="^(admin|editor|reviewer|clinician|member)$")


_attempts = deque()
_lock = Lock()


def login(req: Login):
    # Global bounded admission: per-account rate alone permits a name flood.
    with _lock:
        now = time.monotonic()
        while _attempts and now - _attempts[0] >= 60:
            _attempts.popleft()
        if len(_attempts) >= 12:
            raise HTTPException(429, "登录请求过频")
        _attempts.append(now)
    with transaction() as db:
        row = db.execute("SELECT * FROM users WHERE name=? AND enabled=1", (req.name,)).fetchone()
        # Use one PBKDF2 on misses to reduce trivial username timing disclosure.
        stored = row["password"] if row else "pbkdf2$240000$" + "00" * 16 + "$" + "00" * 32
        valid = password_matches(req.password, stored)
        if not row or not valid:
            raise HTTPException(401, "账号或密码无效")
        if db.execute("SELECT count(*) FROM sessions WHERE expires>?", (int(time.time()),)).fetchone()[0] >= 200:
            raise HTTPException(429, "活动会话容量已满")
        token = secrets.token_urlsafe(32)
        expires = int(time.time()) + 3600
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest_token(token), row["id"], expires))
        audit(db, row["id"], "session.login", row["id"])
        return {"token": token, "expires": expires, "user": {"id": row["id"], "name": row["name"], "role": row["role"]}}


def add_user(db, req, actor):
    from uuid import uuid4
    if db.execute("SELECT count(*) FROM users").fetchone()[0] >= 100:
        raise HTTPException(409, "账号容量已满")
    if db.execute("SELECT 1 FROM users WHERE name=?", (req.name,)).fetchone():
        raise HTTPException(409, "账号已存在")
    user_id = uuid4().hex
    db.execute("INSERT INTO users(id,name,password,role) VALUES(?,?,?,?)",
               (user_id, req.name, password_hash(req.password), req.role))
    audit(db, actor, "user.create", user_id)
    return {"id": user_id, "name": req.name, "role": req.role}


def bootstrap():
    # Run explicitly inside an accepted backend, not on import or deploy.
    name = os.environ.get("APP_BOOTSTRAP_NAME", "")
    password = os.environ.get("APP_BOOTSTRAP_PASSWORD", "")
    req = NewUser(name=name, password=password, role="admin")
    with transaction() as db:
        if db.execute("SELECT count(*) FROM users").fetchone()[0]:
            raise RuntimeError("Existing user store; bootstrap refused")
        add_user(db, req, "bootstrap")


if __name__ == "__main__":
    bootstrap()
    print("Admin provisioned; credentials not printed")
