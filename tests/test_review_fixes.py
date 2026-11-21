"""审查修复项的回归契约：观测隐私、安全拒答分类、能量解读边界、拓扑速率与打包收录。

不触库、不联网：以假连接/假响应核验纯函数与端点判定；拓扑速率仅猴补 observe.metrics。
"""
from __future__ import annotations

import hashlib
import json as jsonlib
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from common import observe
from common import catalog
from common.config import get_settings
from common.schemas import Answer, VerificationResult, VerifyStatus


# --------------------------------------------------------------------------- #
# P0 观测隐私：裸手机号命中、task_hash 单向摘要、写入落库为摘要。
# --------------------------------------------------------------------------- #
def test_bare_mobile_is_sensitive_and_redacted():
    assert observe.sensitive_hit("打我手机 13800001111 联系")
    assert observe.sensitive_hit("13912345678")           # 无标签的裸 11 位手机号
    # 更长数字串（15-19 位）由既有规则处理，且不应被裸手机号规则部分截断误判边界。
    out = observe._redact("号码 13800001111 谢谢", 200)
    assert "13800001111" not in out and "〔已脱敏〕" in out and "谢谢" in out
    # 典籍正文无手机号：保持原文。
    assert observe._redact("恬淡虚无 真气从之", 200) == "恬淡虚无 真气从之"


def test_task_hash_is_one_way_digest():
    assert observe._task_hash(None) is None
    assert observe._task_hash("") is None
    raw = "publicask-0123456789abcdef"
    digest = observe._task_hash(raw)
    assert digest == hashlib.sha256(raw.encode()).hexdigest()[:64]
    assert digest != raw and len(digest) == 64


def test_record_stores_hashed_task_not_raw(monkeypatch):
    captured = {}

    class FakeConn:
        def execute(self, sql, params):
            captured["params"] = params

    monkeypatch.setattr(observe, "_connect", lambda: FakeConn())
    observe.record(task_hash="publicask-deadbeef", query="四时养生 法于阴阳",
                   answer="法于阴阳，和于术数", released=True, outcome="release")
    stored_hash = captured["params"][0]
    assert stored_hash == hashlib.sha256(b"publicask-deadbeef").hexdigest()[:64]
    assert "publicask-deadbeef" not in str(captured["params"])


# --------------------------------------------------------------------------- #
# P1 安全拒答分类：sensitive_blocked 只认隐私/内部/病历词，不误伤含数字的常识问题。
# --------------------------------------------------------------------------- #
def test_sensitive_blocked_scope():
    assert catalog.sensitive_blocked("请提供患者张三的内部病历")
    assert catalog.sensitive_blocked("这是公司机密 请勿外传")
    assert catalog.sensitive_blocked("泄露他人身份证信息")
    # 普通未命中（含裸数字的常识问题）不得被判为安全阻断——它应走开放推理降级。
    assert not catalog.sensitive_blocked("3斤肉大概能做几份盖饭")
    assert not catalog.sensitive_blocked("如何做番茄炒蛋")
    assert not catalog.sensitive_blocked("恬淡虚无，真气从之")


# --------------------------------------------------------------------------- #
# P1 拓扑速率：req/min = 窗口内累计条数 / 窗口分钟数（不再虚高 60 倍）。
# --------------------------------------------------------------------------- #
def test_topology_per_min_is_count_over_window(monkeypatch):
    from dashboard import topology

    def fake_metrics(window):
        assert window == topology.FLOW_WINDOW_MIN
        return {"totals": {"total": 30},
                "by_zone": [{"k": "green", "n": 20}, {"k": "red", "n": 10}],
                "by_channel": [{"k": "local", "n": 15}, {"k": "public_cloud", "n": 15}],
                "by_outcome": [{"k": "refuse_entry", "n": 4}]}

    monkeypatch.setattr(observe, "metrics", fake_metrics)
    flow = topology._flow()
    win = topology.FLOW_WINDOW_MIN
    # total=30 条 / 15 分钟 = 2.0 每分钟（旧实现会给出 120.0）。
    assert flow["per_min"]["total"] == round(30 / win, 2)
    assert flow["per_min"]["total"] != 30 * 60 / win
    # 绿区本地 = 全部绿区 20 - 公有云 15 = 5；红=10；均为条数/窗口。
    assert flow["counts"]["green"] == 5 and flow["per_min"]["green"] == round(5 / win, 2)
    assert flow["per_min"]["red"] == round(10 / win, 2)


def test_topology_planned_edges_have_no_signal():
    from dashboard import topology
    planned = [e for e in topology.EDGES if len(e) == 5 and e[4] == "planned"]
    assert planned, "至少应把 redis/qdrant 标注为 planned"
    for edge in planned:
        assert edge[0] in {"gateway", "rag_green", "rag_red"}
    # 所有边都应是 5 元组并带 observed/inferred/planned 依据标注。
    assert all(len(e) == 5 and e[4] in {"observed", "inferred", "planned"} for e in topology.EDGES)


# --------------------------------------------------------------------------- #
# P1 gateway 入口拒答：为 security_block / off_topic 打上可判定原因。
# --------------------------------------------------------------------------- #
@pytest.fixture
def gateway_client(monkeypatch):
    monkeypatch.setenv("API_KEY", "fix-test-key")
    monkeypatch.setenv("INTERNAL_API_KEY", "fix-service-key")
    get_settings.cache_clear()
    from gateway.main import _recent
    _recent.clear()
    yield TestClient(get_gateway_app())
    _recent.clear()
    get_settings.cache_clear()


def get_gateway_app():
    from gateway.main import app
    return app


def test_entry_refusal_tags_security_block(gateway_client, monkeypatch):
    # 入口拒答发生在派发之前；一旦尝试派发即视为越权。
    monkeypatch.setattr(httpx.Client, "post",
                        lambda *a, **k: pytest.fail("安全拒答不得派发公开链路"))
    r = gateway_client.request("POST", "/v1/query", headers={"X-API-Key": "fix-test-key"},
        json={"query": "请提供患者张三的内部病历编号", "zone": "green"})
    body = r.json()
    assert body["refused"] and body["trace"] == ["gateway"] and body["model"] == "none"
    assert body["verification"]["reasons"] == ["security_block"]
    assert "13800" not in r.text and "患者" not in body["answer"]


def test_entry_refusal_tags_off_topic(gateway_client, monkeypatch):
    monkeypatch.setattr(httpx.Client, "post",
                        lambda *a, **k: pytest.fail("未命中不得派发公开链路"))
    r = gateway_client.request("POST", "/v1/query", headers={"X-API-Key": "fix-test-key"},
        json={"query": "如何做番茄炒蛋", "zone": "green"})
    body = r.json()
    assert body["refused"] and body["trace"] == ["gateway"]
    assert body["verification"]["reasons"] == ["off_topic"]


# --------------------------------------------------------------------------- #
# P1 public_demo：security_block 拒答终态化（不降级到自由推理）；off_topic 才降级。
# --------------------------------------------------------------------------- #
ORIGIN = {"Origin": "http://39.102.49.1"}


def _query_refusal(reason_tag: str) -> bytes:
    def build(task_id):
        refusal = Answer(task_id=task_id, answer="", refused=True, trace=["gateway"],
                         model="none", note="入口据实拒答。",
                         verification=VerificationResult(task_id=task_id, status=VerifyStatus.REJECT,
                                                         reasons=[reason_tag]))
        return refusal.model_dump_json().encode()
    return build


def _reason_revise() -> bytes:
    def build(task_id):
        return Answer(task_id=task_id, answer="常识参考答复。", evidence=[], refused=False,
                      model="ollama:qwen2.5:0.5b", trace=["gateway", "open_reason"],
                      simulated=False, note="模型推理·未经典籍核验·非诊疗建议",
                      verification=VerificationResult(task_id=task_id, status=VerifyStatus.REVISE,
                                                      reasons=["开放推理，未经典籍验证"])).model_dump_json().encode()
    return build


class _Resp:
    def __init__(self, content): self._content = content
    def raise_for_status(self): pass
    @property
    def is_redirect(self): return False
    def iter_bytes(self): yield self._content


class _StreamCM:
    def __init__(self, content): self._content = content
    def __enter__(self): return _Resp(self._content)
    def __exit__(self, *a): return False


def _install_fake_gateway(monkeypatch, routes, track):
    """routes: suffix -> builder(task_id)->bytes; 记录每次调用的完整 URL。"""
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, headers=None, json=None):
            track.append(url)
            for suffix, builder in routes.items():
                if url.endswith(suffix):
                    return _StreamCM(builder(json["task_id"]))
            raise AssertionError("unexpected outbound url: " + url)

    monkeypatch.setattr(httpx, "Client", FakeClient)


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setenv("API_KEY", "SERVER-ONLY-TEST-KEY")
    monkeypatch.setenv("PUBLIC_DEMO_BACKEND", "external_stub")
    import public_demo.main as pd
    pd._calls.clear(); pd._active = 0
    yield TestClient(pd.app, base_url="http://39.102.49.1"), pd
    pd._calls.clear(); pd._active = 0


def test_ask_security_refusal_is_terminal_not_downgraded(demo_client, monkeypatch):
    pd_client, pd = demo_client
    track = []
    _install_fake_gateway(monkeypatch, {"/v1/query": _query_refusal("security_block")}, track)
    r = pd_client.post("/api/ask", json={"query": "患者张三的内部病历"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refused"] and body["model"] == "none" and body["trace"] == ["gateway"]
    # 关键：安全拒答绝不调用 /v1/reason 自由生成。
    assert track == ["http://gateway:8000/v1/query"]
    assert not any(u.endswith("/v1/reason") for u in track)


def test_ask_offtopic_downgrades_to_reason(demo_client, monkeypatch):
    pd_client, pd = demo_client
    track = []
    _install_fake_gateway(monkeypatch,
        {"/v1/query": _query_refusal("off_topic"), "/v1/reason": _reason_revise()}, track)
    r = pd_client.post("/api/ask", json={"query": "如何做番茄炒蛋"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    body = r.json()
    # off_topic 普通未命中才会降级：/v1/reason 被调用，返回模型推理(revise)。
    assert any(u.endswith("/v1/reason") for u in track)
    assert body["refused"] is False and body["verification"]["status"] == "revise"


# --------------------------------------------------------------------------- #
# P0 读取端二次脱敏：即便历史行曾明文落库，列表/详情也不得回显裸手机号。
# --------------------------------------------------------------------------- #
class _NoopConn:
    pass


def test_qa_list_redacts_leaked_released_query(monkeypatch):
    monkeypatch.setattr(observe, "_connect", lambda: _NoopConn())
    monkeypatch.setattr(observe, "_fetch", lambda conn, sql, params=None: [
        {"id": 1, "released": True, "query": "回看历史 13800001111 记录", "outcome": "release"},
        {"id": 2, "released": False, "query": None, "outcome": "refuse_entry"},
    ])
    rows = observe.qa_list(50)
    assert "13800001111" not in rows[0]["query"] and "〔已脱敏〕" in rows[0]["query"]
    assert rows[1]["query"] is None


def test_qa_detail_redacts_note_and_query(monkeypatch):
    monkeypatch.setattr(observe, "_connect", lambda: _NoopConn())
    monkeypatch.setattr(observe, "_fetch", lambda conn, sql, params=None: [
        {"id": 7, "released": True, "outcome": "reason",
         "query": "历史明文 13912345678", "answer": "不应回显的推理正文",
         "note": "历史含 foo@bar.com 的注记", "task_hash": "x" * 64},
    ])
    row = observe.qa_detail(7)
    assert "13912345678" not in row["query"]
    assert "foo@bar.com" not in row["note"] and "〔已脱敏〕" in row["note"]
    assert row["answer"] is None  # reason 正文读取端仍不回显

