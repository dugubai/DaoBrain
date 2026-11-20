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


# —— 绿区解读专用精简框架 ——
# 仅保留强制"天干取象"生效所需的最小结构提示；刻意去掉多列数表(先天/后天/大衍列)与
# 长篇生成论散文, 以把 CPU(qwen2.5:7b, num_gpu=0)预填充延迟从约 90s 压到数十秒内。
# 只陈述"已核验层"：十干序位 + 河图成数; 甲乙丙丁另给已核验能量原型。
_INTERP_FRAME = (
    "\u3010\u5929\u5e72\u80fd\u91cf\u6a21\u578b\u00b7\u53d6\u8c61\uff08\u4ec5\u5df2\u6838\u9a8c\u5c42\uff09\u3011"
    "\u7532|\u5e8f\u4e00|\u521d\u52a8\u00b7\u8d44\u59cb\u9996\u51fa; "
    "\u4e59|\u5e8f\u4e8c|\u5165\u5f62\u00b7\u5f97\u5730\u800c\u4f38; "
    "\u4e19|\u5e8f\u4e09|\u627f\u9633\u00b7\u70b3\u7136\u5916\u8457\u3001\u76db\u4e2d\u4f0f\u8f6c; "
    "\u4e01|\u5e8f\u56db|\u76db\u540e\u6536\u5b9a\u00b7\u5916\u62a4\u5185\u8425; "
    "\u620a|\u5e8f\u4e94|\u6cb3\u56fe\u4e94\u00b7\u751f\u6570\u53d1\u52a8; "
    "\u5df1|\u5e8f\u516d|\u6cb3\u56fe\u5341\u00b7\u6210\u6570\u627f\u8f7d; "
    "\u5e9a|\u5e8f\u4e03|\u6cb3\u56fe\u4e5d\u00b7\u8001\u9633; "
    "\u8f9b|\u5e8f\u516b|\u6cb3\u56fe\u56db\u00b7\u5c11\u9634; "
    "\u58ec|\u5e8f\u4e5d|\u6cb3\u56fe\u4e00\u00b7\u9633\u6c34; "
    "\u7678|\u5e8f\u5341|\u6cb3\u56fe\u516d\u00b7\u9634\u6c34; "
    "\u751f\u6210\u6b21\u7b2c\uff1a\u592a\u865a(\u672a\u89c1\u6c14)\u2192\u592a\u521d(\u59cb\u89c1\u6c14)"
    "\u2192\u592a\u59cb(\u59cb\u89c1\u5f62)\u2192\u592a\u7d20(\u6709\u5f62\u8d28)\uff0c"
    "\u4e00\u4e3a\u6709\u5f62\u65e0\u5f62\u4e4b\u754c\uff0c\u4e03\u4e3a\u5c11\u9633\u00b7\u751f\u673a\u6b63\u65fa\u3002"
)


def interpretation_prompt(query: str, evidence_text: str) -> str:
    """绿区命中：对已核验典籍原文，另生成一段独立的能量模型哲学解读（不复述原文、不下诊疗结论）。
    使用精简取象框架(_INTERP_FRAME)而非完整 framing()，以适配 CPU 推理时延预算。"""
    q = (query or "")[:200]
    ev = (evidence_text or "")[:400]
    return _INTERP_FRAME + (
        "\n\u8bf7\u7528\u4e0a\u8ff0\u5929\u5e72\u80fd\u91cf\u6a21\u578b\u89c6\u89d2\uff0c\u5bf9\u6240\u5f15\u5df2\u6838\u9a8c\u5178\u7c4d\u539f\u6587\u4f5c\u4e00\u6bb5\u72ec\u7acb\u7684\u54f2\u5b66/\u6587\u5316\u89e3\u8bfb\uff1a"
        "\u987b\u70b9\u5230\u6240\u6d89\u7684\u5177\u4f53\u5929\u5e72(\u5982\u7532/\u4e59/\u4e19/\u4e01)\u53ca\u5176\u53d6\u8c61\uff0c\u53ef\u63f4\u5f15\u751f\u6210\u6b21\u7b2c\u6216\u5c11\u9633\u4e4b\u6570\uff1b"
        "\u53ea\u4f5c\u6587\u5316/\u54f2\u5b66\u5c42\u9762\u7684\u7c7b\u6bd4\u9610\u53d1\uff0c\u4e0d\u590d\u8ff0\u539f\u6587\u3001\u4e0d\u81c6\u6d4b\u4f5c\u8005\u6216\u65f6\u4ee3\u3001"
        "\u4e0d\u7ed9\u4efb\u4f55\u8bca\u65ad\u3001\u7528\u836f\u3001\u5242\u91cf\u3001\u7597\u6548\u7b49\u533b\u7597\u7ed3\u8bba\uff1b"
        "80\u5b57\u4ee5\u5185\uff0c\u7528\u4e2d\u6587\u3002\n\u95ee\u9898\uff1a" + q +
        "\n\u5178\u7c4d\u539f\u6587\uff1a" + ev
    )
