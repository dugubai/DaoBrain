"""天干能量模型蒸馏数据入库契约：种子纪律 + 提示视图与种子一致 + 生产提示字节锁定。

覆盖三条不变量：
1) 分层纪律：甲乙丙丁四层齐备且原型已核验；戊己庚辛壬癸仅序位/河图，其余须为 None，禁臆造。
2) energy_lens._INTERP_FRAME（线上绿区/开放推理共用视图）逐干与种子一致：原型只出现在甲乙丙丁。
3) _INTERP_FRAME 与线上黄金字节完全相等，防止重构漂移改变已调优的 CPU 提示（直接影响 503 与延迟）。
"""
from __future__ import annotations

import re

from common import energy_lens, energy_lens_data

VERIFIED = ["甲", "乙", "丙", "丁"]
HETU_ONLY = ["戊", "己", "庚", "辛", "壬", "癸"]

# 线上（7521f8b 起）已验证生效的精简分层取象框架——重构须逐字保持，否则提示延迟/503 回归。
GOLDEN_INTERP_FRAME = (
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


def test_seed_discipline():
    stems = energy_lens_data.STEMS
    assert list(energy_lens_data.STEM_ORDER) == VERIFIED + HETU_ONLY
    for st in VERIFIED:
        v = stems[st]
        assert v["ordinal"] and v["hetu"] and v["xiantian"] and v["houtian"]
        assert v["dayan"] and v["archetype"], f"{st} 已核验干四层与原型须齐备"
    for st in HETU_ONLY:
        v = stems[st]
        assert v["ordinal"] and v["hetu"]
        assert v["xiantian"] is None and v["houtian"] is None
        assert v["dayan"] is None and v["archetype"] is None, f"{st} 未核验层禁臆造"


def test_frame_matches_seed_per_stem():
    frame = energy_lens._INTERP_FRAME
    by_stem = {}
    for line in frame.split("\n"):
        m = re.match(r"^([甲乙丙丁戊己庚辛壬癸])\|", line)
        if m:
            by_stem[m.group(1)] = line
    assert set(by_stem) == set(energy_lens_data.STEM_ORDER)
    for st, line in by_stem.items():
        v = energy_lens_data.STEMS[st]
        assert ("序" + v["ordinal"]) in line
        hetu_num = v["hetu"].split("(")[0]
        branch0 = v["hetu"].split("(")[1][0]
        assert ("河图" + hetu_num + branch0) in line, st
        if st in VERIFIED:
            assert "原型[" in line
            assert "原型[" + v["archetype"].replace("、", "·") + "]" in line, st
        else:
            assert "原型[" not in line and "无核验原型" in line, st + " 禁编原型"


def test_production_prompt_bytes_locked():
    assert energy_lens._INTERP_FRAME == GOLDEN_INTERP_FRAME
    rf = energy_lens.reason_framing()
    assert rf.startswith(GOLDEN_INTERP_FRAME) and "严禁把多个数层并成一个死数" in rf
    ip = energy_lens.interpretation_prompt("恬淡虚无", "恬惔虚无，真气从之")
    assert ip.startswith(GOLDEN_INTERP_FRAME) and "60字以内" in ip
    assert energy_lens.INTERPRETATION_DISCLAIMER == energy_lens_data.INTERPRETATION_DISCLAIMER
