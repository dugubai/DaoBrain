"""天干能量模型 · 解释框架 lens（v1，蒸馏版）。

来源：作者提出的文化/哲学解释框架，蒸馏自候选知识库
`author-framework-candidate-20261116-v1`（其上游为 tiangan-shuyi 数意写作规范快照）。

定位与诚实边界（务必遵守）：
- 这是"分层取数"的哲学解释视角，不是典籍逐字原文，也不是实测物理能量模型。
- 十干各层仅收录规范已明确给出者；缺失层以 None 表示，严禁臆造补全
  （甲乙丙丁较全，戊己庚辛壬癸仅河图配对）。
- 解读只作类比式阐发，不给人固化天干标签，需评估情境、给反例，绝不作医学诊疗/用药/剂量结论。

本模块只产出提示文本与速查数据，不做任何网络或生成调用；由 gateway 调用本地模型生成实际解读。
"""
from __future__ import annotations

# --- 十干速查：仅已核验层；缺层留 None，渲染为「—」，不得编造 ---
STEMS = {
    "甲": {"ordinal": "一", "hetu": "三(天三·初动)", "xiantian": "乾一", "houtian": "震三",
           "dayan": "九", "archetype": "破壳立机、资始首出"},
    "乙": {"ordinal": "二", "hetu": "八(地八·成形)", "xiantian": "兑", "houtian": "巽",
           "dayan": "八", "archetype": "得地而伸、入形而行"},
    "丙": {"ordinal": "三", "hetu": "七(天七·外著)", "xiantian": "离", "houtian": "离九",
           "dayan": "七", "archetype": "承阳成象、炳然外著、盛中伏转"},
    "丁": {"ordinal": "四", "hetu": "二(地二·内养)", "xiantian": "乾一", "houtian": "离九",
           "dayan": "六", "archetype": "盛后收定、外护内营"},
    "戊": {"ordinal": "五", "hetu": "五(天五·生数/发动)", "archetype": None},
    "己": {"ordinal": "六", "hetu": "十(地十·成数/承载)", "archetype": None},
    "庚": {"ordinal": "七", "hetu": "九(天九·老阳/刚成)", "archetype": None},
    "辛": {"ordinal": "八", "hetu": "四(地四·少阴/柔定)", "archetype": None},
    "壬": {"ordinal": "九", "hetu": "一(天一·阳水/初动)", "archetype": None},
    "癸": {"ordinal": "十", "hetu": "六(地六·阴水/已成)", "archetype": None},
}

_STEM_ORDER = "甲乙丙丁戊己庚辛壬癸"

# --- 方法论：分层取数，不可执一 ---
LAYER_PRINCIPLE = (
    "数意不是给天干配一个固定数，而是不同数术体系对同一能量原型的分层取数："
    "序位、河图、先天卦、后天卦、大衍各取一层——"
    "先天卦数论此干『是什么』的本体未发之根据，后天卦数论此干『怎么运行』的已发之法则；"
    "各家所取层面不同，不是此干本体反复变换，更不可把这些层合并成一个死数。"
)

# --- 宇宙生成观（道长课件框架）---
ONTOLOGY = (
    "生成次第：太虚（未见气）→太初（始见气）→太始（始见形）→太素（有形有质）；"
    "『一』为有形无形之分界，『二』为元气初分（轻清者阳、重浊者阴），"
    "『三』为天地人三级能量流转、『三生万物』之成象位；七为少阳之数，生机正旺而未极。"
)

# --- 应用纪律（源自候选库 editorial_limits，硬约束）---
APPLICATION_RULES = (
    "就事论事作类比阐发；不得按职业、年龄或身份给人固化天干标签，须评估情境适用性、"
    "给出反例与替代解释，不预设所有领域皆适用；这是文化与哲学解释框架，"
    "不替代医学诊疗、法律、财务、工程安全或经验数据；凡涉及诊断、用药、剂量、疗效，"
    "一律不作任何医疗结论。"
)

INTERPRETATION_LABEL = "能量模型解读"
INTERPRETATION_DISCLAIMER = (
    "（天干能量模型是作者提出的文化/哲学解释框架，未经典籍语料核验，不替代执业医师诊疗。）"
)


def table_text() -> str:
    lines = ["十干速查（序位｜河图｜先天｜后天｜大衍｜能量原型）："]
    for st in _STEM_ORDER:
        v = STEMS[st]

        def g(k):
            val = v.get(k)
            return val if val else "—"

        lines.append(
            "{s}：序{a} 河图{b} 先天{c} 后天{d} 大衍{e} 原型[{f}]".format(
                s=st, a=g("ordinal"), b=g("hetu"), c=g("xiantian"),
                d=g("houtian"), e=g("dayan"), f=g("archetype"))
        )
    return "\n".join(lines)


def framing() -> str:
    """共享的方法论底座文本（供 reason 与绿区解读两种提示复用）。"""
    return "【天干能量模型·解释框架】" + LAYER_PRINCIPLE + \
        " 生成观：" + ONTOLOGY + " " + table_text() + " 应用纪律：" + APPLICATION_RULES


def reason_framing() -> str:
    """开放推理层 /v1/reason：让答案从能量模型视角组织（通用指令之前注入）。"""
    return framing() + (
        " 请用上述能量模型视角（天干能量原型与分层取数）解读用户的问题："
        "只做文化/哲学层面的解释，可点出所涉天干与其取数层面的象征义，"
        "80字以内，用中文，不给诊疗建议，不确定就说不确定。"
    )


def interpretation_prompt(query: str, evidence_text: str) -> str:
    """绿区命中：对已核验典籍原文，另生成一段独立的能量模型哲学解读（不复述原文、不下诊疗结论）。"""
    q = (query or "")[:200]
    ev = (evidence_text or "")[:600]
    return framing() + (
        "\n下面是一句已核验的典籍养生原文与用户问题。仅从上述天干能量模型视角，"
        "对这段原文的养生意涵作一段独立的哲学解读：不复述原文、不臆测、不作任何医疗结论，"
        "80字以内，用中文。\n问题：" + q + "\n典籍原文：" + ev
    )
