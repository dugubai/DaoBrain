"""文化档案的规范模拟语料与保守词法检索；不是向量检索。

哈希覆盖原文与版本，可信校验逐字段对照规范记录。
公开正文与模拟编辑规则分开；红区全部为纯合成演示。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from common.corpus_fixtures import FIXTURES
from common.schemas import Provenance, Retrieved, SourceType, Zone

VERSION = "simulation-v1"
DISCLAIMER = "本内容仅为文化辅助参考，不替代执业医师诊疗。"
STUB_LABEL = "（本地模拟：仅摘录证据，未调用真实模型）"
TUNED_LABEL = "（本地红区模拟：非真实训练模型，仅摘录合成样例）"
OLLAMA_LABEL = "（本地 Ollama：仅摘录核验后的证据）"
EXTERNAL_LABEL = "（外部模型模拟：仅摘录可信公开证据，未调用外部API）"
RETRIEVAL_LABEL = "（公开检索模拟：仅摘录可信公开证据）"
_LABELS = (STUB_LABEL, TUNED_LABEL, OLLAMA_LABEL, EXTERNAL_LABEL, RETRIEVAL_LABEL)


def _entry(source_id: str, title: str, locator: str, text: str, quote: str,
           source_type: SourceType, zone: Zone) -> Retrieved:
    return Retrieved(
        text=text, zone=zone,
        provenance=Provenance(
            source_id=source_id, title=title, locator=locator, quote=quote,
            source_type=source_type, version=VERSION,
            content_hash=hashlib.sha256((text + "\n" + VERSION).encode("utf-8")).hexdigest(),
        ),
    )


# 统一构造不可变来源的规范证据，查询时仍只返回深拷贝。
_SEED = tuple(
    _entry(f.source_id, f.title, f.locator, f.text, f.quote, f.source_type, f.zone)
    for f in FIXTURES
)
_KEYWORDS = {f.source_id: f.keywords for f in FIXTURES}
_STOP = {"如何", "什么", "这个", "那个", "可以", "是否", "我们", "用户", "信息", "材料",
         "的文", "关于", "请问", "查询", "the", "and", "what", "is", "of", "to"}
_BLOCKED = re.compile(
    r"秘密|保密|机密|隐私|个人信息|个人资料|未公开|内部|红区|病历|患者|病例|姓名|"
    r"身份证|手机号|电话号码|住址|地址|邮箱|邮件|账号|密码|密钥|令牌|薪资|工资|"
    r"secret|private|confidential|internal|password|token|api.?key|pii|ssn|patient|"
    r"e.?mail|phone|address|salary|https?://|www\.|@|\d",
    re.IGNORECASE,
)
# 安全敏感词（用于区分"安全拒答"与"普通未命中"）：只含隐私/内部/病历类关键词及其英文等价词，
# 刻意不含裸数字 \d 与邮箱/电话/URL/@ 等格式型 PII —— 后者由 observe.sensitive_hit 在推理层兜底。
_SECURITY_TERMS = re.compile(
    r"秘密|保密|机密|隐私|个人信息|个人资料|未公开|内部|红区|病历|患者|病例|姓名|"
    r"身份证|手机号|电话号码|住址|密码|密钥|令牌|薪资|工资|"
    r"secret|private|confidential|internal|password|token|api.?key|pii|ssn|patient|record",
    re.IGNORECASE,
)
_PUBLIC_TOPICS = tuple(sorted({
    "黄帝内经", "素问", "上古天真论", "四气调神大论", "四气调神", "四时养生", "四季养生",
    "法于阴阳", "和于术数", "恬淡虚无", "真气从之", "精神内守", "病安从来", "春三月",
    "此谓发陈", "文化辅助参考", "文化参考", "文化辅助", "平台用途", "用途", "免责",
    "公开文化faq", "模拟公开faq", "文化faq", "faq", "不替代执业医师诊疗",
} | {topic for f in FIXTURES if f.zone == Zone.GREEN for topic in f.public_topics},
    key=len, reverse=True))
_PUBLIC_FILLERS = tuple(sorted({
    "请问", "请", "介绍一下", "介绍", "解释一下", "解释", "说明", "查询", "检索", "引用",
    "原文", "出处", "引文", "摘录", "如何理解", "如何", "什么是", "是什么", "什么", "含义",
    "有哪些", "哪些", "的", "中", "关于", "与", "和", "以及", "一下", "吗", "呢", "公开", "模拟",
}, key=len, reverse=True))


def catalog(zone: Zone) -> list[Retrieved]:
    """Return isolated copies; UNKNOWN never acquires a default public corpus."""
    return [e.model_copy(deep=True) for e in _SEED if zone in (Zone.GREEN, Zone.RED) and e.zone == zone]


def trusted(e: Retrieved) -> bool:
    """A hash, source id, or client-supplied zone alone is never sufficient."""
    if not isinstance(e, Retrieved):
        return False
    return any(e.model_dump(mode="json") == canonical.model_dump(mode="json") for canonical in _SEED)


def _tokens(text: str) -> set[str]:
    result: set[str] = set()
    for run in re.findall(r"[\u3400-\u9fff]+|[a-z][a-z0-9_-]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", run):
            result.update(run[i:i + 2] for i in range(len(run) - 1))
        else:
            result.add(run)
    return result - _STOP


def public_query(query: str) -> bool:
    """Strict topic grammar, not a sensitivity classifier or zone authorization.

    Unknown residual words fail closed, even when a public topic is also present.
    """
    if not isinstance(query, str) or not 0 < len(query) <= 2000:
        return False
    normalized = unicodedata.normalize("NFKC", query).lower()
    if _BLOCKED.search(normalized):
        return False
    normalized = re.sub(r"[\s《》〈〉·，。！？、：；‘’“”\"'(),.!?:;\-]", "", normalized)
    found = False
    for topic in _PUBLIC_TOPICS:
        if topic in normalized:
            found = True
            normalized = normalized.replace(topic, "")
    for filler in _PUBLIC_FILLERS:
        normalized = normalized.replace(filler, "")
    return found and not normalized


def sensitive_blocked(query: str) -> bool:
    """安全敏感判定（内部/病历/隐私/证件类词）：区别于 public_query 的话题语法。

    供 gateway 标记"安全拒答"（终态、不得降级到自由推理）；不返回授权，仅作分类。
    """
    if not isinstance(query, str) or not query:
        return False
    return bool(_SECURITY_TERMS.search(unicodedata.normalize("NFKC", query).lower()))


def search(query: str, top_k: int, zone: Zone) -> list[Retrieved]:
    """Keyword/bigram ranking with no no-hit fallback or single-character match."""
    if not isinstance(query, str) or not 0 < len(query) <= 2000:
        raise ValueError("query must contain 1..2000 characters")
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("top_k must be an integer in 1..20")
    if zone == Zone.GREEN and not public_query(query):
        return []
    q = unicodedata.normalize("NFKC", query).lower()
    tokens = _tokens(q)
    ranked: list[tuple[int, Retrieved]] = []
    for item in catalog(zone):
        hay = item.text + item.provenance.title + item.provenance.locator + item.provenance.quote
        keywords = sum(term in q for term in _KEYWORDS[item.provenance.source_id])
        overlap = len(tokens & _tokens(hay))
        # Two overlapping bigrams minimum unless a complete curated keyword matches.
        score = keywords * 10 + overlap if keywords or overlap >= 2 else 0
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in ranked[:top_k]]


def extract(evidence: list[Retrieved], label: str, max_chars: int = 4096) -> tuple[str, list[Retrieved]]:
    """Bounded deterministic rendering, keeping only the evidence actually quoted."""
    if label not in _LABELS:
        raise ValueError("unsupported extraction label")
    lines: list[str] = []
    selected: list[Retrieved] = []
    seen: set[str] = set()
    for item in evidence:
        sid = item.provenance.source_id
        if sid in seen or len(selected) >= 4:
            continue
        # Include the canonical source text as well as its quotation so the shared
        # literal-evidence verifier can independently check the extraction.
        line = f"[{sid}]「{item.provenance.quote}」 {item.text}"
        if len("\n".join([label, *lines, line, DISCLAIMER])) > max_chars:
            break
        seen.add(sid)
        lines.append(line)
        selected.append(item.model_copy(deep=True))
    if not selected:
        raise ValueError("no evidence fits the output limit")
    return "\n".join([label, *lines, DISCLAIMER]), selected


def supported_quotes(answer: str, evidence: list[Retrieved]) -> bool:
    """Reject unsupported prose, altered quotations, and unused evidence.

    Only exact canonical quotes, or the controlled extraction format, are accepted.
    This is a deliberately narrow simulation rule, not a general fact checker.
    """
    if not answer or len(answer) > 4096 or not evidence or not all(trusted(e) for e in evidence):
        return False
    by_id = {e.provenance.source_id: e.provenance.quote for e in evidence}
    texts = {e.provenance.source_id: e.text for e in evidence}
    if len(by_id) != len(evidence):
        return False
    lines = answer.strip().splitlines()
    if lines and lines[0] in _LABELS:
        lines.pop(0)
    if lines and lines[-1] == DISCLAIMER:
        lines.pop()
    used: set[str] = set()
    for line in lines:
        match = re.fullmatch(r"\[([a-zA-Z0-9_-]+)\]「([^\n]+?)」(?: ([^\n]+))?", line)
        if match:
            sid, quote, source_text = match.groups()
            if (sid not in by_id or quote != by_id[sid] or sid in used
                    or (source_text is not None and source_text != texts[sid])):
                return False
        else:
            matches = [sid for sid, quote in by_id.items() if line in (quote, f"「{quote}」")]
            if len(matches) != 1 or matches[0] in used:
                return False
            sid = matches[0]
        used.add(sid)
    return bool(used) and used == set(by_id)
