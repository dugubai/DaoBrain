"""独立问答的真实Ollama适配；仅允许固定本地运行器，不含模拟回退。"""
from __future__ import annotations

import logging
import os
import time

import httpx

from common.qa import (MODEL_NAME, MAX_RESPONSE_BYTES, JsonRejected, QaResult,
                       decode_model, make_prompt, retrieve_qa)


class ModelFailure(JsonRejected):
    """内部受控失败类别；公开异常文本不含问题、响应、端点或异常原文。"""
    def __init__(self, category: str, elapsed_ms: int, status: int = 0,
                 response_bytes: int = 0):
        super().__init__("真实问答模型不可用或结果未通过引用与格式核验")
        self.diagnostic = {"category": category, "elapsed_ms": elapsed_ms,
                           "http_status": status, "response_bytes": response_bytes}

# 【通用骨架】服务器配置和用户载荷彻底分离；不接收客户端模型/URL/密钥。
_ALLOWED_ENDPOINTS = {
    "http://qa_ollama:11434": "http://qa_ollama:11434/api/generate",
    "http://127.0.0.1:11435": "http://127.0.0.1:11435/api/generate",
}


def endpoint() -> str:
    base = os.environ.get("QA_OLLAMA_URL", "http://qa_ollama:11434")
    if base not in _ALLOWED_ENDPOINTS:
        raise JsonRejected("问答运行器必须是明确配置的本地隔离端点")
    return _ALLOWED_ENDPOINTS[base]


def infer(question: str, task_id: str) -> QaResult:
    """【任务扩展】一次真实生成；失败不重试、不修JSON、不摘录回退。"""
    evidence = retrieve_qa(question)
    if not evidence:
        raise JsonRejected("公开经典库中没有足够相关证据")
    prompt, schema = make_prompt(question, evidence)
    payload = {
        "model": MODEL_NAME, "prompt": prompt, "format": schema, "stream": False,
        "keep_alive": 0,
        "options": {"num_predict": 256, "num_ctx": 1024, "num_batch": 8,
                    "num_gpu": 0, "num_thread": 1, "use_mmap": True,
                    "temperature": 0},
    }
    url = endpoint()  # 在任何传输之前拒绝非法配置。
    started = time.monotonic()
    status = 0
    body = bytearray()

    def failed(category: str) -> ModelFailure:
        failure = ModelFailure(category, max(0, int((time.monotonic() - started) * 1000)),
                               status, min(len(body), MAX_RESPONSE_BYTES + 1))
        # 内部日志仅固定类别和计数；不记录问题、任务ID、响应、URL或异常原文。
        value = failure.diagnostic
        logging.getLogger(__name__).warning(
            "qa_model_failure category=%s elapsed_ms=%d http_status=%d response_bytes=%d",
            value["category"], value["elapsed_ms"], value["http_status"], value["response_bytes"],
        )
        return failure

    try:
        # 【通用骨架】不读环境代理、不重定向、不携带业务凭证；响应实际限长。
        # read timeout并非硬总期限；流读取和完成后另外检查累计时间。
        with httpx.Client(timeout=httpx.Timeout(120, connect=3), trust_env=False,
                          follow_redirects=False) as client:
            with client.stream("POST", url, json=payload) as response:
                status = response.status_code
                response.raise_for_status()
                if response.is_redirect or response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                    raise failed("response_protocol")
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise failed("response_too_large")
                    body.extend(chunk)
                    if time.monotonic() - started > 120:
                        raise failed("elapsed_limit")
        if time.monotonic() - started > 120:
            raise failed("elapsed_limit")
        try:
            return decode_model(bytes(body), question, task_id, evidence, MODEL_NAME)
        except (ValueError, TypeError, UnicodeError):
            raise failed("result_rejected") from None
    except ModelFailure:
        raise
    except httpx.TimeoutException:
        raise failed("transport_timeout") from None
    except httpx.HTTPStatusError:
        raise failed("http_status") from None
    except httpx.HTTPError:
        raise failed("transport_error") from None
    except (ValueError, TypeError, UnicodeError):
        raise failed("response_protocol") from None
