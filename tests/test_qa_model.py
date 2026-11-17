"""真实模型适配器的离线传输测试；不会加载模型或访问外部网络。"""
import json
from contextlib import contextmanager

import httpx
import pytest

from common.qa import JsonRejected, retrieve_qa
from public_qa import model

QUESTION = "为什么教别人也会帮助自己学习？"


def good_bytes():
    ref = retrieve_qa(QUESTION)[0].provenance
    nested = {"explanation": "教与学相互促进，教别人能帮助我们发现自己的理解不足，通过反思和进一步学习改进认识。",
              "citations": [{"source_id": ref.source_id, "quote": ref.quote}]}
    return json.dumps({"model": "qwen2.5:0.5b", "done": True, "done_reason": "stop",
                       "eval_count": 60, "prompt_eval_count": 220,
                       "response": json.dumps(nested, ensure_ascii=False)}, ensure_ascii=False).encode()


class FakeClient:
    def __init__(self, *, body=None, status=200, headers=None, failure=None):
        self.body = good_bytes() if body is None else body
        self.status = status
        self.headers = {"Content-Type": "application/json"} if headers is None else headers
        self.failure = failure
        self.calls = []
        self.options = None

    def factory(self, **kwargs):
        self.options = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @contextmanager
    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.failure:
            raise self.failure
        yield httpx.Response(self.status, headers=self.headers, content=self.body,
                             request=httpx.Request(method, url))


def test_actual_adapter_single_call_and_no_proxy_or_redirect(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    result = model.infer(QUESTION, "qa-transport-test")
    assert result.model == "ollama:qwen2.5:0.5b"
    assert len(fake.calls) == 1
    assert fake.options["trust_env"] is False
    assert fake.options["follow_redirects"] is False
    method, url, kwargs = fake.calls[0]
    assert method == "POST" and url == "http://qa_ollama:11434/api/generate"
    assert "headers" not in kwargs
    assert kwargs["json"]["keep_alive"] == 0
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["options"]["num_gpu"] == 0
    assert kwargs["json"]["format"]["additionalProperties"] is False


@pytest.mark.parametrize("status", [301, 302, 307, 400, 429, 500, 503])
def test_failures_no_retry_or_stub_fallback(monkeypatch, status):
    fake = FakeClient(status=status)
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    with pytest.raises(model.ModelFailure) as error:
        model.infer(QUESTION, "qa-transport-test")
    assert error.value.diagnostic["category"] == "http_status"
    assert error.value.diagnostic["http_status"] == status
    assert len(fake.calls) == 1


@pytest.mark.parametrize("body", [b'not json', b'{"response":"invalid"}', b'x' * 32769])
def test_invalid_model_response_never_returns_excerpt(monkeypatch, body):
    fake = FakeClient(body=body)
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    with pytest.raises(JsonRejected):
        model.infer(QUESTION, "qa-transport-test")
    assert len(fake.calls) == 1


def test_timeout_never_retries(monkeypatch):
    fake = FakeClient(failure=httpx.ReadTimeout("offline test"))
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    with pytest.raises(JsonRejected):
        model.infer(QUESTION, "qa-transport-test")
    assert len(fake.calls) == 1


@pytest.mark.parametrize("endpoint", [
    "https://evil.invalid", "http://192.168.1.1:11434", "http://qa_ollama:11434/path",
    "http://qa_ollama:11434?secret=x", "http://user:pass@qa_ollama:11434",
    "http://127.0.0.1:80", "http://qa_ollama.evil.invalid:11434",
])
def test_arbitrary_endpoints_rejected_before_transport(monkeypatch, endpoint):
    monkeypatch.setenv("QA_OLLAMA_URL", endpoint)
    fake = FakeClient()
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    with pytest.raises(JsonRejected):
        model.infer(QUESTION, "qa-transport-test")
    assert fake.calls == []


def test_sensitive_or_unmatched_input_does_not_call_model(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    for question in ("教学相长及我的病历", "火星探测机械零件的意义是什么？"):
        with pytest.raises(JsonRejected):
            model.infer(question, "qa-transport-test")
    assert fake.calls == []


def test_content_type_mismatch_fails_closed(monkeypatch):
    fake = FakeClient(headers={"Content-Type": "text/html"})
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    with pytest.raises(model.ModelFailure) as error:
        model.infer(QUESTION, "qa-transport-test")
    assert error.value.diagnostic["category"] == "response_protocol"
    assert len(fake.calls) == 1


@pytest.mark.parametrize("failure,category", [
    (httpx.ReadTimeout("private payload must not leak"), "transport_timeout"),
    (httpx.ConnectTimeout("private endpoint must not leak"), "transport_timeout"),
    (httpx.ConnectError("private header must not leak"), "transport_error"),
])
def test_safe_diagnostic_categories_never_include_payload(monkeypatch, failure, category):
    fake = FakeClient(failure=failure)
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    with pytest.raises(model.ModelFailure) as error:
        model.infer(QUESTION, "qa-transport-test")
    diagnostic = error.value.diagnostic
    assert set(diagnostic) == {"category", "elapsed_ms", "http_status", "response_bytes"}
    assert diagnostic["category"] == category
    assert type(diagnostic["elapsed_ms"]) is int and diagnostic["elapsed_ms"] >= 0
    assert QUESTION not in str(error.value) and "private" not in str(error.value)
    assert "private" not in json.dumps(diagnostic)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("body,category", [
    (b"not json", "result_rejected"),
    (b"x" * 32769, "response_too_large"),
])
def test_result_and_size_rejection_have_distinct_categories(monkeypatch, body, category):
    fake = FakeClient(body=body)
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    with pytest.raises(model.ModelFailure) as error:
        model.infer(QUESTION, "qa-transport-test")
    assert error.value.diagnostic["category"] == category
    assert error.value.diagnostic["response_bytes"] <= 32768
    assert len(fake.calls) == 1


def test_elapsed_limit_checked_before_result_is_released(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    times = iter([0, 121, 121])
    monkeypatch.setattr(model.time, "monotonic", lambda: next(times))
    with pytest.raises(model.ModelFailure) as error:
        model.infer(QUESTION, "qa-transport-test")
    assert error.value.diagnostic["category"] == "elapsed_limit"
    assert len(fake.calls) == 1


def test_empty_late_response_is_not_accepted(monkeypatch):
    fake = FakeClient(body=b"")
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    times = iter([0, 121, 121])
    monkeypatch.setattr(model.time, "monotonic", lambda: next(times))
    with pytest.raises(model.ModelFailure) as error:
        model.infer(QUESTION, "qa-transport-test")
    assert error.value.diagnostic["category"] == "elapsed_limit"


def test_private_runtime_log_contains_only_safe_categories_and_numbers(monkeypatch, caplog):
    fake = FakeClient(body=b"private-response-secret", status=500)
    monkeypatch.setattr(model.httpx, "Client", fake.factory)
    with pytest.raises(model.ModelFailure):
        model.infer(QUESTION, "private-task-id")
    assert "qa_model_failure category=http_status" in caplog.text
    assert "http_status=500" in caplog.text
    assert "private-response-secret" not in caplog.text
    assert "private-task-id" not in caplog.text
    assert QUESTION not in caplog.text
    assert "qa_ollama" not in caplog.text
