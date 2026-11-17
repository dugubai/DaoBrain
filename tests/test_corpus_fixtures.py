"""文化语料逐条检索、来源完整性、跨区隔离与有界摘录回归。"""
from __future__ import annotations

import hashlib
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from common.catalog import (DISCLAIMER, STUB_LABEL, TUNED_LABEL, VERSION,
                            catalog, extract, public_query, search,
                            supported_quotes, trusted)
from common.config import get_settings
from common.corpus_fixtures import FIXTURES
from common.schemas import SourceType, Zone
from corpus_kb.main import app as corpus_app
from daobrain_tuned.main import app as tuned_app
from local_model.main import app as local_app
from rag_green.main import app as green_app
from rag_public.main import app as public_app
from rag_red.main import app as red_app


GREEN = tuple(f for f in FIXTURES if f.zone == Zone.GREEN)
RED = tuple(f for f in FIXTURES if f.zone == Zone.RED)
OLD_IDS = {"suwen-1", "suwen-2", "suwen-3", "faq-0", "synthetic-red-1", "synthetic-red-2"}
HEADERS = {"X-Internal-Key": "corpus-fixture-test-key"}


def _canonical(fixture):
    return next(e for e in catalog(fixture.zone) if e.provenance.source_id == fixture.source_id)


@pytest.fixture
def route_settings(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", HEADERS["X-Internal-Key"])
    monkeypatch.setenv("LOCAL_MODEL_BACKEND", "stub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_counts_identity_and_category_contract():
    assert len({f.source_id for f in FIXTURES}) == len(FIXTURES)
    assert OLD_IDS <= {f.source_id for f in FIXTURES}
    assert len(GREEN) == 22 and len(RED) == 12
    assert 15 <= sum(f.source_id not in OLD_IDS for f in GREEN) <= 25
    assert 8 <= sum(f.source_id not in OLD_IDS for f in RED) <= 12
    assert Counter(f.source_type for f in GREEN) == {
        SourceType.PUBLIC_CLASSIC: 14, SourceType.FAQ: 4, SourceType.APPROVED: 4,
    }
    for f in GREEN:
        if f.source_type != SourceType.PUBLIC_CLASSIC:
            assert "模拟公开" in f.title and "非典籍原文" in f.title
    for f in RED:
        assert "纯合成演示" in f.text and "无真实用户秘密" in f.text
        assert f.source_id.startswith("synthetic-red-")
        assert f.source_type == SourceType.ORIGINAL and not f.public_topics


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.source_id)
def test_each_record_expected_source_rank_and_exact_hash(fixture):
    hits = search(fixture.expected_query, 4, fixture.zone)
    assert tuple(e.provenance.source_id for e in hits[:len(fixture.expected_sources)]) == fixture.expected_sources
    assert all(e.zone == fixture.zone and trusted(e) for e in hits)
    item = _canonical(fixture)
    assert item.provenance.quote in item.text
    assert item.provenance.content_hash == hashlib.sha256(
        (item.text + "\n" + VERSION).encode("utf-8")).hexdigest()
    assert search(fixture.expected_query, 1, fixture.zone)[0] == hits[0]


@pytest.mark.parametrize("fixture,term", [
    (f, term) for f in FIXTURES for term in sorted(set(f.keywords + f.public_topics))
], ids=[f"{f.source_id}:{term}" for f in FIXTURES for term in sorted(set(f.keywords + f.public_topics))])
def test_every_declared_keyword_and_topic_is_retrievable(fixture, term):
    if fixture.zone == Zone.GREEN:
        assert public_query(term)
    assert fixture.source_id in {e.provenance.source_id for e in search(term, 20, fixture.zone)}


# 原文核实结果独立列明，防止把模拟规范或自创讲解混进 PUBLIC_CLASSIC。
VERIFIED_QUOTES = {
    "suwen-1": "法于阴阳，和于术数",
    "suwen-2": "春三月，此谓发陈，天地俱生，万物以荣",
    "suwen-3": "恬惔虚无，真气从之，精神内守，病安从来",
    "suwen-food": "食饮有节",
    "suwen-routine": "起居有常，不妄作劳",
    "suwen-summer": "夏三月，此谓蕃秀，天地气交，万物华实",
    "suwen-autumn": "秋三月，此谓容平，天气以急，地气以明",
    "suwen-winter": "冬三月，此谓闭藏，水冰地坼，无扰乎阳",
    "suwen-seasonal": "春夏养阳，秋冬养阴",
    "xueji-jade": "玉不琢，不成器。人不学，不知道",
    "xueji-learning": "虽有嘉肴，弗食，不知其旨也。虽有至道，弗学，不知其善也",
    "xueji-reflection": "学然后知不足，教然后知困",
    "xueji-mutual": "教学相长",
    "xueji-friends": "独学而无友，则孤陋而寡闻",
}


def test_classic_quotes_are_verified_text_not_modern_explanation():
    classics = [f for f in GREEN if f.source_type == SourceType.PUBLIC_CLASSIC]
    assert {f.source_id for f in classics} == set(VERIFIED_QUOTES)
    for f in classics:
        assert f.quote == VERIFIED_QUOTES[f.source_id]
        assert f.text == f"《{f.title}·{f.locator}》：{f.quote}。"
    assert "四时养生是顺应四季的文化主题" not in _canonical(GREEN[1]).text
    assert search("恬淡虚无", 1, Zone.GREEN)[0].provenance.source_id == "suwen-3"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.source_id)
@pytest.mark.parametrize("field", ["text", "zone", "source_id", "title", "locator", "quote",
                                   "source_type", "version", "content_hash"])
def test_every_record_rejects_every_field_tamper(fixture, field):
    item = _canonical(fixture)
    if field == "zone":
        item.zone = Zone.RED if fixture.zone == Zone.GREEN else Zone.GREEN
    elif field == "text":
        item.text += "篡改"
    elif field == "source_type":
        item.provenance.source_type = (SourceType.FAQ if fixture.source_type == SourceType.ORIGINAL
                                       else SourceType.ORIGINAL)
    elif field == "content_hash":
        item.provenance.content_hash = "0" * 64
    else:
        setattr(item.provenance, field, getattr(item.provenance, field) + "篡改")
    assert not trusted(item)
    label = STUB_LABEL if fixture.zone == Zone.GREEN else TUNED_LABEL
    answer, evidence = extract([item], label)
    assert not supported_quotes(answer, evidence)
    assert trusted(_canonical(fixture))


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.source_id)
def test_recomputed_hash_does_not_establish_trust(fixture):
    item = _canonical(fixture)
    item.text += "伪造的编者说明"
    item.provenance.content_hash = hashlib.sha256(
        (item.text + "\n" + VERSION).encode("utf-8")).hexdigest()
    assert not trusted(item)
    item = _canonical(fixture)
    item.provenance.version = "simulation-forged"
    item.provenance.content_hash = hashlib.sha256(
        (item.text + "\n" + item.provenance.version).encode("utf-8")).hexdigest()
    assert not trusted(item)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.source_id)
def test_unknown_empty_and_cross_zone_catalog_isolation(fixture):
    assert catalog(Zone.UNKNOWN) == []
    assert search(fixture.expected_query, 4, Zone.UNKNOWN) == []
    opposite = Zone.RED if fixture.zone == Zone.GREEN else Zone.GREEN
    assert fixture.source_id not in {e.provenance.source_id for e in catalog(opposite)}
    assert all(e.zone == opposite for e in search(fixture.expected_query, 20, opposite))
    if fixture.zone == Zone.RED:
        assert not public_query(fixture.expected_query)
        assert search(fixture.expected_query, 4, Zone.GREEN) == []


@pytest.mark.parametrize("fixture", GREEN, ids=lambda f: f.source_id)
@pytest.mark.parametrize("suffix", ["未知星系词汇", "待核暗语", "XYZunrelated"])
def test_public_topics_never_license_unknown_residual_words(fixture, suffix):
    query = f"{fixture.expected_query} {suffix}"
    assert not public_query(query)
    assert search(query, 4, Zone.GREEN) == []


@pytest.mark.parametrize("suffix", [
    "秘密", "保密", "机密", "隐私", "个人信息", "个人资料", "未公开", "内部", "红区",
    "病历", "患者", "病例", "姓名", "身份证", "手机号", "电话号码", "住址", "地址",
    "邮箱", "邮件", "账号", "密码", "密钥", "令牌", "薪资", "工资", "secret", "private",
    "confidential", "internal", "password", "token", "api_key", "pii", "ssn", "patient",
    "email", "phone", "address", "salary", "https://example.invalid", "www.example.invalid",
    "@", "7", "７", "①", "ｐａｓｓｗｏｒｄ",
])
def test_expanded_public_corpus_keeps_sensitive_numeric_and_url_blocking(suffix):
    query = f"资料授权范围 {suffix}"
    assert not public_query(query)
    assert search(query, 4, Zone.GREEN) == []


@pytest.mark.parametrize("zone", list(Zone))
@pytest.mark.parametrize("query", ["流程", "的", "阴", "天", "未知星系数据库", "孤", "zzz_unrelated"])
def test_unrelated_and_single_character_queries_never_fallback(zone, query):
    assert search(query, 4, zone) == []


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.source_id)
def test_rag_endpoints_share_only_the_public_corpus(route_settings, fixture):
    apps = (public_app, green_app) if fixture.zone == Zone.GREEN else (red_app,)
    results = []
    for app in apps:
        response = TestClient(app).post("/retrieve", headers=HEADERS,
                                       json={"query": fixture.expected_query, "top_k": 4})
        assert response.status_code == 200
        payload = response.json()
        assert tuple(e["provenance"]["source_id"] for e in payload[:len(fixture.expected_sources)]) == fixture.expected_sources
        assert all(e["zone"] == fixture.zone.value for e in payload)
        results.append(payload)
    corpus_client = TestClient(corpus_app)
    corpus_result = corpus_client.post("/search", headers=HEADERS,
                                       json={"query": fixture.expected_query, "top_k": 4})
    assert corpus_result.status_code == 200
    source_path = f"/sources/{fixture.source_id}"
    if fixture.zone == Zone.GREEN:
        assert results[0] == results[1] == corpus_result.json()
        item = _canonical(fixture)
        params = {"version": VERSION, "content_hash": item.provenance.content_hash}
        source = corpus_client.get(source_path, headers=HEADERS, params=params)
        assert source.status_code == 200 and source.json() == item.model_dump(mode="json")
        for invalid in ({"version": "simulation-forged"}, {"content_hash": "0" * 64}):
            assert corpus_client.get(source_path, headers=HEADERS, params=invalid).status_code == 404
    else:
        assert corpus_result.json() == []
        assert corpus_client.get(source_path, headers=HEADERS).status_code == 404
        for app in (public_app, green_app):
            assert TestClient(app).post("/retrieve", headers=HEADERS,
                                       json={"query": fixture.expected_query}).json() == []


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.source_id)
def test_new_evidence_cannot_cross_inference_zones(route_settings, fixture):
    item = _canonical(fixture).model_dump(mode="json")
    if fixture.zone == Zone.RED:
        app, path, zone, query = local_app, "/infer", Zone.GREEN, "四时养生"
    else:
        app, path, zone, query = tuned_app, "/reason", Zone.RED, "虚构流程甲"
    response = TestClient(app).post(path, headers=HEADERS, json={
        "task_id": "fixture-isolation", "query": query, "zone": zone.value, "evidence": [item],
    })
    assert response.status_code == 403


@pytest.mark.parametrize("zone,label", [(Zone.GREEN, STUB_LABEL), (Zone.RED, TUNED_LABEL)])
def test_expanded_catalog_extraction_stays_at_four_and_within_output_limit(zone, label):
    evidence = catalog(zone)
    answer, selected = extract(evidence, label, 4096)
    assert len(selected) == 4 and len(answer) <= 4096
    assert answer.endswith(DISCLAIMER)
    assert supported_quotes(answer, selected)
    assert not supported_quotes(answer, evidence)
    assert selected == evidence[:4]
    assert extract([evidence[0]] * 6 + evidence[1:], label)[1] == evidence[:4]

    one_answer, _ = extract(evidence[:1], label)
    exact_limit = len(one_answer)
    bounded, bounded_evidence = extract(evidence, label, exact_limit)
    assert len(bounded) == exact_limit and len(bounded_evidence) == 1
    assert supported_quotes(bounded, bounded_evidence)
    with pytest.raises(ValueError):
        extract(evidence, label, exact_limit - 1)
    selected[0].provenance.quote += "篡改"
    assert trusted(catalog(zone)[0])


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.source_id)
def test_supported_quotes_rejects_altered_quote_and_unsupported_prose(fixture):
    label = STUB_LABEL if fixture.zone == Zone.GREEN else TUNED_LABEL
    evidence = [_canonical(fixture)]
    answer, selected = extract(evidence, label)
    assert supported_quotes(answer, selected)
    assert not supported_quotes(answer.replace("」", "伪造」", 1), selected)
    assert not supported_quotes(answer + "\n无证据的自创解释", selected)
    assert not supported_quotes(answer, selected + selected)
    assert not supported_quotes("甲" * 4097, selected)


def test_extraction_rejects_empty_evidence_and_unknown_label():
    with pytest.raises(ValueError):
        extract([], STUB_LABEL)
    with pytest.raises(ValueError):
        extract(catalog(Zone.GREEN), "伪造标签")
