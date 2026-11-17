"""结构化真实模型适配器的单元测试；Mock不能代替服务器实测。"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from common.catalog import OLLAMA_LABEL, search, supported_quotes
from common.config import get_settings
from common.schemas import Candidate, Zone
from local_model.main import app

REAL_CLIENT = httpx.Client
KEY = "model-selection-unit-key"


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for key, value in {
        "INTERNAL_API_KEY": KEY, "LOCAL_MODEL_BACKEND": "ollama",
        "LOCAL_MODEL_NAME": "qwen2.5:0.5b", "MODEL_OUTPUT_PROTOCOL": "select",
        "MODEL_CONTEXT_TOKENS": "1024", "MODEL_CPU_THREADS": "2",
        "MODEL_MAX_OUTPUT_TOKENS": "96", "OLLAMA_URL": "http://ollama:11434",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def call(monkeypatch, response_body):
    evidence = search("法于阴阳", 4, Zone.GREEN)
    def handler(request):
        payload = json.loads(request.content)
        assert payload["options"]["num_ctx"] == 1024
        assert payload["options"]["num_thread"] == 2
        assert payload["options"]["num_batch"] == 8
        assert payload["options"]["num_gpu"] == 0 and payload["options"]["use_mmap"] is True
        assert payload["format"]["additionalProperties"] is False
        field = payload["format"]["properties"]["source_ids"]
        assert field["minItems"] == field["maxItems"] == 1
        assert field["items"]["enum"] == [e.provenance.source_id for e in evidence]
        assert payload["stream"] is False
        assert request.url.host == "ollama"
        return httpx.Response(200, json=response_body)
    def factory(*args, **kwargs):
        assert kwargs.get("trust_env") is False
        assert kwargs.get("follow_redirects") is False
        return REAL_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "Client", factory)
    return TestClient(app).post("/infer", json={
        "task_id": "selection", "zone": "green", "query": "法于阴阳",
        "evidence": [e.model_dump(mode="json") for e in evidence],
    }, headers={"X-Internal-Key": KEY})


def valid_response():
    return {"model": "qwen2.5:0.5b", "done": True, "eval_count": 18,
            "prompt_eval_count": 90, "response": '{"source_ids":["suwen-1"]}'}


def test_selected_evidence_only(monkeypatch):
    result = call(monkeypatch, valid_response())
    assert result.status_code == 200, result.text
    cand = Candidate.model_validate(result.json())
    assert cand.model == "ollama:qwen2.5:0.5b"
    assert [e.provenance.source_id for e in cand.evidence] == ["suwen-1"]
    assert cand.answer.startswith(OLLAMA_LABEL)
    assert supported_quotes(cand.answer, cand.evidence)


@pytest.mark.parametrize("choice", [
    '{"source_ids":[]}', '{"source_ids":["not-retrieved"]}',
    '{"source_ids":["synthetic-red-1"]}', '{"source_ids":["suwen-1","suwen-1"]}',
    '{"source_ids":[1]}', '{"source_ids":["suwen-1"],"answer":"编造"}',
    '{"source_ids":"suwen-1"}', '[]', '非JSON',
])
def test_bad_selection_fails_closed(monkeypatch, choice):
    response = valid_response()
    response["response"] = choice
    result = call(monkeypatch, response)
    assert result.status_code == 503
    assert "answer" not in result.json()


@pytest.mark.parametrize("field,value", [
    ("model", "fake"), ("done", False), ("eval_count", 0), ("eval_count", True),
    ("eval_count", "18"), ("prompt_eval_count", 0), ("prompt_eval_count", None),
    ("error", "unavailable"),
])
def test_no_unverified_model_success(monkeypatch, field, value):
    response = valid_response()
    response[field] = value
    assert call(monkeypatch, response).status_code == 503
