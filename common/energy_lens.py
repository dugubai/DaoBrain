"""天干能量模型 · 解释框架 lens（v1，蒸馏版）。

核心蒸馏数据入库为 common/energy_model/distilled_stems.json，由 common.energy_lens_data
装载并做一致性校验；本模块从该唯一真源取 STEMS 与方法论常量，只负责拼装提示文本，
不重复维护数据、不触网、不调用生成模型；由 gateway 调用本地模型生成实际解读。

蒸馏来源：作者提出的文化/哲学解释框架，蒸馏自候选知识库
`author-framework-candidate-20261116-v1`（上游为 tiangan-shuyi 数意写作规范快照）。

诚实边界（务必遵守）：
- 这是"分层取数"的哲学解释视角，不是典籍逐字原文，也不是实测物理能量模型。
- 十干各层仅收录规范已明确给出者；缺失层以 None 表示，严禁臆造补全
  （甲乙丙丁较全，戊己庚辛壬癸仅河图配对）。
- 解读只作类比式阐发，不给人固化天干标签，需评估情境、给反例，绝不作医学诊疗/用药/剂量结论。
"""
from __future__ import annotations

from common.energy_lens_data import (
    APPLICATION_RULES,
    INTERPRETATION_DISCLAIMER,
    INTERPRETATION_LABEL,
    LAYER_PRINCIPLE,
    ONTOLOGY,
    STEMS,
    STEM_ORDER,
)

# 十干展示顺序由蒸馏数据定义；保留旧内部名以兼容既有引用。
_STEM_ORDER = STEM_ORDER


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
    return _INTERP_FRAME + (
        " 请用上述天干能量模型分层取象视角解读用户的问题：先点出其能量性状，"
        "据此只选一个最相应的天干并声明所取层面（序位或河图或原型），引用该干已核验取象为据、"
        "再给一句相应之理；甲乙丙丁方可言原型，戊己庚辛壬癸不得臆造原型；"
        "严禁把多个数层并成一个死数；只作文化/哲学解释，不给诊疗建议，不确定就说不确定，80字以内，中文。"
    )


# —— 绿区解读与开放推理共用：分层取象"有理有据"框架 ——
# 只保留已核验层，并强制：单一取干 + 声明层面 + 引用该干已核验取象为依据 + 给出相应之理；
# 严禁把多个数层并成一个死数（混层）、严禁为戊己庚辛壬癸臆造能量原型。结构化且精简，
# 既让天干取象稳定生效，又把 CPU(qwen2.5:7b, num_gpu=0)预填充延迟压到数十秒内，消除 reason 503。
# 本视图与 common/energy_model/distilled_stems.json 的取象须保持一致（tests/test_energy_lens_data.py 校验）。
_INTERP_FRAME = (
    "【天干能量模型·分层取象（仅已核验层）】\n"
    "甲|序一|河图三天·初动|原型[破壳立机·资始首出];\n"
    "乙|序二|河图八地·成形|原型[得地而伸·入形而行];\n"
    "丙|序三|河图七天·外著|原型[承阳成象·炳然外著·盛中伏转];\n"
    "丁|序四|河图二地·内养|原型[盛后收定·外护内营];\n"
    "戊|序五|河图五天·生数发动（无核验原型，勿编象）;\n"
    "己|序六|河图十地·成数承载（无核验原型）;\n"
    "庚|序七|河图九天·老阳（无核验原型）;\n"
    "辛|序八|河图四地·少阴（无核验原型）;\n"
    "壬|序九|河图一天·阳水（无核验原型）;\n"
    "癸|序十|河图六地·阴水（无核验原型）;\n"
    "分层原则：序位＝生成次第、河图＝成数、先天＝本体未发、后天＝运行已发；"
    "每次只取一层，不得把多层并成一个死数（如既称庚老阳又称少阳即属混层，禁用）。"
)


def interpretation_prompt(query: str, evidence_text: str) -> str:
    """绿区命中：对已核验典籍原文，另生成一段独立、有理有据的能量模型哲学解读。
    只作文化/哲学类比，不复述原文、不下诊疗结论；解读与逐字核验后的答案分离。"""
    q = (query or "")[:200]
    ev = (evidence_text or "")[:400]
    return _INTERP_FRAME + (
        "\n请对上述已核验典籍原文作一段能量模型哲学解读，务求有理有据：\n"
        "(1) 先用一句点出原文之义与其能量性状（如动/伸/明/收/藏/承载/内养等）；\n"
        "(2) 据此只选一个最相应的天干，声明所取层面（序位或河图或原型），"
        "并引用该干在上表中已核验的取象作为依据；甲乙丙丁方可言『原型』，"
        "戊己庚辛壬癸无核验原型、只可引其序位或河图数、严禁臆造能量原型；\n"
        "(3) 说明原文性状为何与该干取象相应（一句类比之理）；\n"
        "(4) 只作文化/哲学类比，不复述原文、不猜作者时代、不给诊断用药剂量等医疗结论；"
        "60字以内，中文。\n问题：" + q + "\n典籍原文：" + ev
    )
