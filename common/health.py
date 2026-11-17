"""各服务共用的健康检查路由。"""
from fastapi import APIRouter


def health_router(module: str) -> APIRouter:
    r = APIRouter()

    @r.get("/health")
    def health():
        return {"status": "ok", "module": module}

    return r
