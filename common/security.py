"""模拟部署鉴权。共享服务密钥不能代替生产的独立身份和网络隔离。"""
import hashlib
import hmac
import json
import time
from fastapi import Header, HTTPException
from common.config import get_settings


def _check(expected: str, supplied: str | None) -> None:
    if not expected:
        raise HTTPException(503, "服务密钥未配置")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(401, "鉴权失败")


def require_api(x_api_key: str | None = Header(default=None)) -> str:
    _check(get_settings().api_key, x_api_key)
    return "simulation-user"


def require_internal(x_internal_key: str | None = Header(default=None)) -> None:
    _check(get_settings().internal_api_key, x_internal_key)


def internal_headers() -> dict[str, str]:
    return {"X-Internal-Key": get_settings().internal_api_key}


def _digest(task_id: str, query: str, evidence: list) -> str:
    data = {"task_id": task_id, "query": query,
            "evidence": [e.model_dump(mode="json") for e in evidence]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sign_approval(task_id: str, query: str, evidence: list) -> str:
    secret = get_settings().approval_secret
    if not secret:
        raise HTTPException(503, "服务端批准密钥未配置")
    expiry = str(int(time.time()) + 60)
    payload = expiry + ":" + _digest(task_id, query, evidence)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return expiry + "." + signature


def check_approval(proof: str, task_id: str, query: str, evidence: list) -> bool:
    secret = get_settings().approval_secret
    if not secret:
        return False
    try:
        expiry, signature = proof.split(".", 1)
        if not int(time.time()) <= int(expiry) <= int(time.time()) + 65:
            return False
        payload = expiry + ":" + _digest(task_id, query, evidence)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (ValueError, TypeError):
        return False
