"""天干能量模型 · 蒸馏数据装载与校验（DaoBrain 核心初始数据的唯一真源）。

蒸馏数据以 JSON 形式随源码入库：common/energy_model/distilled_stems.json。
本模块只做「读取 + 一致性校验 + 构造运行期视图」，不触网、不调用模型。
energy_lens.py 从这里取 STEMS 及方法论常量，杜绝同一份数据在两处漂移。

诚实边界：甲乙丙丁四层（序位/河图/先天/后天/大衍）与能量原型均已核验入册；
戊己庚辛壬癸仅序位与河图偏取有据，其余层规范未给出、置 None，严禁臆造补全。
"""
from __future__ import annotations

import json
from pathlib import Path

SEED_PATH = Path(__file__).resolve().parent / "energy_model" / "distilled_stems.json"

_VERIFIED_FULL = ("甲", "乙", "丙", "丁")            # 四层齐备且能量原型已核验
_HETU_ONLY = ("戊", "己", "庚", "辛", "壬", "癸")     # 仅序位+河图，禁编原型


def _load_raw() -> dict:
    with SEED_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _hetu_text(h: dict) -> str:
    return "{num}({branch}·{note})".format(**h)


def _validate(raw: dict) -> None:
    if raw.get("schema") != "daobrain-energy-lens-seed/v1":
        raise ValueError("蒸馏数据 schema 非预期：" + str(raw.get("schema")))
    order = raw["stem_order"]
    stems = raw["stems"]
    if "".join(order) != "甲乙丙丁戊己庚辛壬癸":
        raise ValueError("stem_order 必须为十干正序")
    if set(order) != set(stems):
        raise ValueError("stem_order 与 stems 键不一致")
    meta = raw["meta"]
    if meta.get("status") != "author-framework/unverified":
        raise ValueError("蒸馏数据须标注 status=author-framework/unverified")
    if "不替代" not in meta.get("disclaimer", ""):
        raise ValueError("蒸馏数据缺少免责/边界声明")
    for st in _VERIFIED_FULL:
        v = stems[st]
        for key in ("ordinal", "xiantian", "houtian", "dayan", "archetype"):
            if not v.get(key):
                raise ValueError(st + " 属已核验干，缺 " + key + " 层")
        if v.get("archetype_verified") is not True:
            raise ValueError(st + " 原型须标 archetype_verified=True")
        if not v["hetu"].get("note"):
            raise ValueError(st + " 河图缺注")
    for st in _HETU_ONLY:
        v = stems[st]
        if not v.get("ordinal") or not v["hetu"].get("note"):
            raise ValueError(st + " 缺序位或河图，违背蒸馏完整性")
        for key in ("xiantian", "houtian", "dayan", "archetype"):
            if v.get(key) is not None:
                raise ValueError(st + " 未核验层须为 None，禁止臆造：" + key + "=" + repr(v.get(key)))
        if v.get("archetype_verified") is not False:
            raise ValueError(st + " 未核验干 archetype_verified 须为 False")


_RAW = _load_raw()
_validate(_RAW)

META = _RAW["meta"]
STEMS = {
    st: {
        "ordinal": v["ordinal"],
        "hetu": _hetu_text(v["hetu"]),
        "xiantian": v["xiantian"],
        "houtian": v["houtian"],
        "dayan": v["dayan"],
        "archetype": v["archetype"],
    }
    for st, v in _RAW["stems"].items()
}
STEM_ORDER = tuple(_RAW["stem_order"])
LAYER_PRINCIPLE = META["layer_principle"]
ONTOLOGY = META["ontology"]
APPLICATION_RULES = META["application_rules"]
INTERPRETATION_LABEL = META["interpretation_label"]
INTERPRETATION_DISCLAIMER = META["disclaimer"]
SAME_WUXING_HETU_RULE = META["same_wuxing_hetu_rule"]
HONESTY_BOUNDARY = META["honesty_boundary"]
