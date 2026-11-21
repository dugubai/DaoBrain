"""开放推理层须对隐私/凭证/外链类查询据实拒答，且在调用本地模型之前拦截。"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from common.config import Settings, get_settings
from gateway import main

API_HEADERS = {"X-API-Key": "reason-priv-test"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key, value in {
        "API_KEY": "reason-priv-test", "INTERNAL_API_KEY": "i",
        "APPROVAL_SECRET": "s", "OPEN_REASON_ENABLED": "true",
        "LOCAL_MODEL_BACKEND": "ollama", "LOCAL_MODEL_NAME": "qwen2.5:0.5b",
        "OLLAMA_URL": "http://ollama:11434", "MODEL_TIMEOUT_SECONDS": "5",
    }.items():
        monkeypatch.setenv(key, value)
    main._recent.clear()
    get_settings.cache_clear()
    yield TestClient(main.app)
    main._recent.clear()
    get_settings.cache_clear()


def _no_model(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("敏感查询绝不应触发模型调用")
    monkeypatch.setattr(httpx, "Client", boom)


def test_reason_refuses_card_and_password_before_model(client, monkeypatch):
    _no_model(monkeypatch)
    resp = client.post("/v1/reason", headers=API_HEADERS,
        json={"query": "我银行卡 6222020000000000 密码 hunter2 帮我看看", "zone": "unknown"})
    assert resp.status_code == 200
    assert resp.json()["refused"] is True


def test_reason_refuses_id_card_and_url(client, monkeypatch):
    _no_model(monkeypatch)
    resp = client.post("/v1/reason", headers=API_HEADERS,
        json={"query": "身份证 110101199001011234 详情见 https://x.io/p", "zone": "unknown"})
    assert resp.json()["refused"] is True


def test_reason_still_answers_benign_philosophy(client, monkeypatch):
    # 良性文化问题不得被敏感门过度拦截：证明门控精准、不伤开放问答可用性。
    called = {}

    class FakeStream:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def raise_for_status(self):
            return None
        @property
        def is_redirect(self):
            return False
        def iter_bytes(self):
            yield '{"response": "天地气相于", "done": true}'.encode("utf-8")

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def stream(self, method, url, json=None):
            called["url"] = url
            return FakeStream()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    resp = client.post("/v1/reason", headers=API_HEADERS,
        json={"query": "春三月为什么讲发陈", "zone": "unknown"})
    assert resp.status_code == 200
    assert resp.json()["refused"] is False
    assert "url" in called  # 良性问题确实走到了模型调用
