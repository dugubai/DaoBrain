"""Approved card references, not uploads, models, RED retrieval or a vector DB.

All mutations take the caller's SQLite transaction. Public reads revalidate both
sides of the card/reference link and emit canonical classic evidence only.
"""
import sqlite3

from fastapi import APIRouter, HTTPException, Query, Request

from application_hub.store import audit, create, get, listing, transaction, update
from common.catalog import catalog, public_query, search, trusted
from common.schemas import SourceType, Zone

router = APIRouter()
RETRIEVAL = "lexical_prototype"
NOTE = "仅对已独立审核的规范公开原典引用作词法检索；非向量或模型检索，无命中不兜底。"


def approved_evidence(payload):
    """The full catalog record and every provenance field must match exactly."""
    if not isinstance(payload, dict) or payload.get("zone") != "green":
        raise HTTPException(403, "只有可信 GREEN 原典可以批准")
    for evidence in catalog(Zone.GREEN):
        if (evidence.zone == Zone.GREEN
                and evidence.provenance.source_type == SourceType.PUBLIC_CLASSIC
                and trusted(evidence)
                and payload.get("text") == evidence.text
                and payload.get("provenance") == evidence.provenance.model_dump(mode="json")
                and payload.get("annotation") in ("", evidence.text, evidence.provenance.quote)):
            return evidence
    raise HTTPException(403, "批准须逐字匹配规范原典全文及完整出处，批注只能为空或原文")


def _linked_entry(db, card_id):
    # Serialized BEGIN IMMEDIATE + a persistent per-card uniqueness constraint.
    # The ID itself is always allocated by store.create, never from a request.
    try:
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS library_card_unique "
                   "ON objects(json_extract(payload,'$.card_id')) WHERE kind='library'")
    except sqlite3.IntegrityError:
        raise HTTPException(409, "知识库字卡关联不唯一") from None
    row = db.execute("SELECT id FROM objects WHERE kind='library' "
                     "AND json_extract(payload,'$.card_id')=?", (card_id,)).fetchone()
    return get(db, "library", row["id"]) if row else None


def publish_card(db, card, evidence, actor):
    """Called only after the approved card update, in that same transaction."""
    if (card["status"] != "approved" or actor == card["owner"]
            or card["payload"].get("reviewed_by") != actor
            or card["payload"].get("updated_by") == actor
            or approved_evidence(card["payload"]).model_dump(mode="json")
            != evidence.model_dump(mode="json")):
        raise HTTPException(403, "入库需要独立审核及规范原典")
    payload = {"card_id": card["id"], "card_version": card["version"], "reviewed_by": actor,
               **evidence.model_dump(mode="json")}
    entry = _linked_entry(db, card["id"])
    if entry is None:
        entry = create(db, "library", card["owner"], payload, status="approved")
        audit(db, actor, "library.publish", entry["id"])
        return entry
    if entry["owner"] != card["owner"]:
        raise HTTPException(409, "知识库关联提交者不一致")
    return update(db, entry, entry["version"], payload, "approved", actor, "library.publish")


def withdraw_card(db, card, actor):
    """Retain old evidence and ID; withdrawal participates in the card write."""
    entry = _linked_entry(db, card["id"])
    if entry is not None and entry["status"] != "withdrawn":
        return update(db, entry, entry["version"], entry["payload"], "withdrawn",
                      actor, "library.withdraw")
    return entry


def _public_entry(db, entry):
    if entry["status"] != "approved" or not isinstance(entry["payload"], dict):
        return None
    payload = entry["payload"]
    card_id = payload.get("card_id")
    if not isinstance(card_id, str):
        return None
    try:
        card = get(db, "card", card_id)
        if (card["status"] != "approved" or card["owner"] != entry["owner"]
                or type(payload.get("card_version")) is not int
                or card["version"] != payload["card_version"]
                or not isinstance(card["payload"], dict)):
            return None
        reviewer = card["payload"].get("reviewed_by")
        if (not isinstance(reviewer, str) or not reviewer
                or reviewer in (card["owner"], card["payload"].get("updated_by"))
                or payload.get("reviewed_by") != reviewer):
            return None
        evidence = approved_evidence(card["payload"])
    except HTTPException:
        return None
    canonical = evidence.model_dump(mode="json")
    if any(payload.get(key) != canonical[key] for key in ("text", "zone", "provenance")):
        return None
    provenance = canonical["provenance"]
    # No owner identity, review comments, free annotations or draft data escape.
    return {"id": entry["id"], "origin": "approved_library", "text": evidence.text,
            "zone": "green", "source_id": provenance["source_id"],
            "source_type": provenance["source_type"], "title": provenance["title"],
            "locator": provenance["locator"], "version": provenance["version"],
            "content_hash": provenance["content_hash"], "provenance": provenance,
            "card_id": card_id, "card_version": card["version"],
            "library_version": entry["version"]}


def _query_fields(request, allowed):
    names = [name for name, _ in request.query_params.multi_items()]
    if set(names) - allowed or len(names) != len(set(names)):
        raise HTTPException(422, "公开查询不接受额外范围、语料或重复参数")


@router.get("/api/library/search")
def search_library(request: Request,
                   query: str = Query(min_length=1, max_length=2000),
                   top_k: int = Query(default=4, ge=1, le=20)):
    _query_fields(request, {"query", "top_k"})
    # Reuse the r4 strict public topic grammar without widening its boundaries.
    if not public_query(query):
        raise HTTPException(403, "只允许规范公开文化主题查询")
    matches = [e for e in search(query, 20, Zone.GREEN)
               if e.zone == Zone.GREEN and e.provenance.source_type == SourceType.PUBLIC_CLASSIC
               and trusted(e)]
    order = {e.provenance.source_id: rank for rank, e in enumerate(matches)}
    items = []
    with transaction() as db:
        # The shared store caps *all* objects at 2000; do not silently search
        # just its default first page or use a fixed classic as a no-hit fallback.
        for entry in listing(db, "library", limit=2000, status="approved"):
            item = _public_entry(db, entry)
            if item is not None and item["source_id"] in order:
                items.append(item)
    items.sort(key=lambda item: (order[item["source_id"]], item["id"]))
    return {"items": items[:top_k], "retrieval": RETRIEVAL, "note": NOTE}


@router.get("/api/library/sources/{source_id}")
def read_source(source_id: str, request: Request,
                version: str | None = Query(default=None, min_length=1, max_length=64),
                content_hash: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$")):
    """source_id is the server-issued library reference ID, not a corpus ID."""
    _query_fields(request, {"version", "content_hash"})
    with transaction() as db:
        entry = get(db, "library", source_id)
        item = _public_entry(db, entry)
        if (item is None or (version is not None and version != item["version"])
                or (content_hash is not None and content_hash != item["content_hash"])):
            raise HTTPException(404, "已审核原典引用不存在或已撤回")
        return item
