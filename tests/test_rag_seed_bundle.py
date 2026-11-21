"""RAG 初始数据种子的可移植性契约：与当前源码零漂移、hash 为目录原生、逐条检索 rank1。

覆盖三条不变量：
1) 已提交的 common/rag_seed/corpus_seed.json 必须与 scripts/export_rag_seed.build_seed()
   现算结果完全一致——语料或版本一旦变动而未重导种子即失败（跨环境部署前的零漂移保证）。
2) 每条 content_hash 必须等于运行期 catalog._SEED 的 provenance.content_hash，text 同源。
3) 每条 expected_query 用 search(top_k=4) 在对应分区必须命中本条 source_id 为 rank1，
   与 docs/rag-data.md 的检索预期表一致；分区计数固定 22 GREEN / 12 RED / 34 总。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from common import catalog
from common.schemas import Zone

ROOT = Path(__file__).resolve().parents[1]
SEED_FILE = ROOT / "common" / "rag_seed" / "corpus_seed.json"


def _committed():
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def _generator():
    spec = importlib.util.spec_from_file_location(
        "export_rag_seed", ROOT / "scripts" / "export_rag_seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_bundle_matches_generator_and_counts():
    fresh = _generator().build_seed()
    committed = _committed()
    assert committed == fresh, "提交种子与当前源码语料漂移，请重跑 scripts/export_rag_seed.py"
    assert committed["schema"] == "daobrain-rag-seed/v1"
    assert committed["version"] == catalog.VERSION == "simulation-v1"
    assert committed["counts"] == {"total": 34, "green": 22, "red": 12}


def test_seed_hashes_are_catalog_native():
    by_id = {r.provenance.source_id: r for r in catalog._SEED}
    for rec in _committed()["records"]:
        assert rec["source_id"] in by_id
        prov = by_id[rec["source_id"]].provenance
        assert rec["content_hash"] == prov.content_hash, rec["source_id"]
        assert rec["text"] == by_id[rec["source_id"]].text, rec["source_id"]


def test_seed_expected_query_rank1():
    for rec in _committed()["records"]:
        result = catalog.search(rec["expected_query"], 4, Zone(rec["zone"]))
        assert result, f"{rec['source_id']} 的 expected_query 无命中：{rec['expected_query']}"
        assert result[0].provenance.source_id == rec["source_id"], (
            f"{rec['source_id']} 未排 rank1，首位={result[0].provenance.source_id}")
