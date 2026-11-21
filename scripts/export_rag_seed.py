"""导出文化档案 RAG 模拟语料的可移植初始数据种子（带逐条内容 hash 清单）。

用途：把运行期权威语料（common/catalog._SEED + common/corpus_fixtures.FIXTURES）
凝练成一份 JSON 清单，随源码包跨环境部署，供别的环境做完整性校验与审计。
内容 hash 直接取 catalog._SEED 的 provenance.content_hash，与运行期逐字节一致，
本文件不是运行期数据源；真正检索仍从 corpus_fixtures 构造。

    python scripts/export_rag_seed.py [输出路径]
默认写到 common/rag_seed/corpus_seed.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "common" / "rag_seed" / "corpus_seed.json"


def build_seed() -> dict:
    sys.path.insert(0, str(ROOT))
    from common import catalog
    from common.corpus_fixtures import FIXTURES

    by_id = {r.provenance.source_id: r for r in catalog._SEED}
    records = []
    green = red = 0
    for f in FIXTURES:
        r = by_id[f.source_id]
        prov = r.provenance
        assert prov.content_hash, f"{f.source_id} 缺内容 hash"
        zone = f.zone.value
        if zone == "green":
            green += 1
        elif zone == "red":
            red += 1
        records.append({
            "source_id": f.source_id,
            "title": f.title,
            "locator": f.locator,
            "quote": f.quote,
            "text": f.text,
            "zone": zone,
            "source_type": f.source_type.value,
            "content_hash": prov.content_hash,
            "keywords": list(f.keywords),
            "expected_query": f.expected_query,
            "expected_sources": list(f.expected_sources),
            "public_topics": list(f.public_topics),
        })
    return {
        "schema": "daobrain-rag-seed/v1",
        "version": catalog.VERSION,
        "generated_by": "scripts/export_rag_seed.py",
        "note": ("文化档案RAG模拟语料的可移植初始数据种子；逐条 content_hash 与运行期 "
                 "catalog._SEED 一致。用于跨环境部署完整性校验与审计，非运行期数据源，"
                 "真实检索仍由 common/corpus_fixtures.py 构造。全部为已核实公开典籍正文"
                 "或纯合成演示，不含真实人名、病历或个人秘密。"),
        "counts": {"total": len(records), "green": green, "red": red},
        "records": records,
    }


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(build_seed(), ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    out.write_text(text, encoding="utf-8")
    print("WROTE", out, "records=", len(json.loads(text)["records"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
