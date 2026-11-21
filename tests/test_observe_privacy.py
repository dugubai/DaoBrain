"""观测落库隐私契约：命中判定、脱敏、按处置裁剪正文，并验证 record 实际落库参数。

不触库、不联网：以假连接捕获 INSERT 参数来核验写端策略；读端策略在 qa_detail 逻辑内。
"""
from __future__ import annotations

from common import observe


def test_sensitive_hit_and_clean_text_safe():
    assert observe.sensitive_hit("我的身份证110101199001011234")
    assert observe.sensitive_hit("email me at foo@bar.com now")
    assert observe.sensitive_hit("用 token abcdef123 登录")
    assert observe.sensitive_hit("见 https://evil.example/x")
    assert not observe.sensitive_hit("恬淡虚无，真气从之，精神内守")
    assert not observe.sensitive_hit("教学相长 独学而无友")


def test_redact_replaces_but_keeps_surroundings():
    out = observe._redact("联系 foo@bar.com 详谈", 200)
    assert "@" not in out and "〔已脱敏〕" in out and "详谈" in out
    assert observe._redact(None, 200) is None
    assert observe._redact("教学相长", 200) == "教学相长"


def test_privacy_shape_by_outcome():
    q, a, n = observe._privacy("患者身份证 110101199001011234", "诊疗建议", None,
                               released=False, outcome="refuse_redline")
    assert q is None and a is None                     # 被拒：不落正文
    q, a, n = observe._privacy("普通开放问题", "自由推理正文", None,
                               released=True, outcome="reason")
    assert a is None and "普通开放问题" in q           # 推理正文不落库
    q, a, n = observe._privacy("查 foo@bar.com 资料", "已核验公开正文", None,
                               released=True, outcome="release")
    assert "@" not in q and a == "已核验公开正文"       # 释放答案保留但脱敏


def test_record_drops_refused_body(monkeypatch):
    captured = {}

    class FakeConn:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

    monkeypatch.setattr(observe, "_connect", lambda: FakeConn())
    observe.record(query="患者 13800001111 身份证 110101199001011234",
                   answer="建议服用某某药", released=False, outcome="refuse_verify")
    # 参数序：task_hash,persona,query,zone,channel,route_target,tier,verify,released,outcome,model,ec,eids,lat,note,answer
    assert captured["params"][2] is None   # query
    assert captured["params"][15] is None  # answer


def test_record_redacts_released_green_answer(monkeypatch):
    captured = {}

    class FakeConn:
        def execute(self, sql, params):
            captured["params"] = params

    monkeypatch.setattr(observe, "_connect", lambda: FakeConn())
    observe.record(query="联系 admin@corp.io 索取", answer="法于阴阳，和于术数",
                   released=True, outcome="release")
    q, a = captured["params"][2], captured["params"][15]
    assert "@" not in q
    assert a == "法于阴阳，和于术数"
