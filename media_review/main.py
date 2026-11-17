"""Persisted public-quotation review; no uploads, downloads or AI vision.

Image/video records contain only public-source metadata and canonical quotations,
never media bytes. Approving such a record does not certify any unseen asset.
Interpretation of images/video returns 501; text reading is deterministic and
explicitly not an AI interpretation. This router awaits hub integration.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import Field, field_validator

from application_hub.security import Input, Principal, Review, roles
from application_hub.store import audit, create, get, listing, transaction, update
from common.catalog import catalog
from common.schemas import SourceType, Zone

router = APIRouter(prefix="/api/media", tags=["public-media-review"])
KIND = "media_record"
NOTE = ("仅公开经典逐字引文及本地独立审核；无上传、下载、OCR、AI视觉或视频理解。"
        "image/video 记录只有引文元数据，审核不表示看过任何真实媒体。")


class MediaCreate(Input):
    source_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["text", "image", "video"]
    reviewer_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    text: str | None = Field(default=None, max_length=4000)
    subtitles: str | None = Field(default=None, max_length=4000)


class MediaReview(Review):
    @field_validator("comment")
    @classmethod
    def code_only(cls, value):
        if value not in ("", "quote-confirmed", "quote-mismatch"):
            raise ValueError("审核备注仅支持固定代号，不接受自由正文")
        return value


def _source(source_id):
    for entry in catalog(Zone.GREEN):
        if (entry.provenance.source_id == source_id and
                entry.provenance.source_type == SourceType.PUBLIC_CLASSIC):
            return entry
    raise HTTPException(422, "仅支持 common.catalog 中的公开经典 source_id")


def _canonical_content(source, kind):
    quote = source.provenance.quote
    if not quote or len(quote) > 4000:
        raise HTTPException(422, "公开引文超过原型界限")
    return {"source_id": source.provenance.source_id, "kind": kind,
            "text": None if kind == "video" else quote,
            "subtitles": quote if kind == "video" else None,
            "provenance": source.provenance.model_dump(mode="json"),
            "media_asset_present": False}


def _verify_content(payload):
    canonical = _canonical_content(_source(payload["source_id"]), payload["kind"])
    if any(payload.get(key) != value for key, value in canonical.items()):
        raise HTTPException(409, "来源或引文版本不再匹配；禁止继续审核或阅读")
    return canonical


def _visible(item, user):
    return (item["owner"] == user.id or
            (user.role == "reviewer" and item["payload"]["reviewer_id"] == user.id))


def _permitted(db, record_id, user):
    item = get(db, KIND, record_id)
    if not _visible(item, user):
        raise HTTPException(404, "记录不存在")
    return item


@router.post("", status_code=201)
def create_record(req: MediaCreate, user: Principal = Depends(roles("editor"))):
    source = _source(req.source_id)
    quote = source.provenance.quote
    if any(value is not None and value != quote for value in (req.text, req.subtitles)):
        raise HTTPException(422, "正文或字幕必须逐字等于所选公开经典引文，不接受改写或附加文字")
    if req.subtitles is not None and req.kind != "video":
        raise HTTPException(422, "字幕字段只适用于视频引文元数据")
    with transaction() as db:
        reviewer = db.execute("SELECT id FROM users WHERE id=? AND role='reviewer' AND enabled=1",
                              (req.reviewer_id,)).fetchone()
        if reviewer is None:
            raise HTTPException(422, "需要有效的独立 reviewer 账号")
        if req.reviewer_id == user.id:
            raise HTTPException(403, "禁止自审")
        payload = {**_canonical_content(source, req.kind), "reviewer_id": req.reviewer_id,
                   "review": None, "note": NOTE}
        return create(db, KIND, user.id, payload)


@router.get("")
def list_records(offset: int = Query(default=0, ge=0, le=2000),
                 limit: int = Query(default=20, ge=1, le=50),
                 status: str | None = Query(default=None, pattern=r"^(draft|approved|rejected)$"),
                 user: Principal = Depends(roles("editor", "reviewer"))):
    with transaction() as db:
        items = listing(db, KIND, owner=None if user.role == "reviewer" else user.id,
                        limit=2000, status=status)
        permitted = [item for item in items if _visible(item, user)]
        return {"items": permitted[offset:offset + limit], "total": len(permitted), "note": NOTE}


@router.get("/{record_id}")
def get_record(record_id: str = Path(pattern=r"^[a-f0-9]{32}$"),
               user: Principal = Depends(roles("editor", "reviewer"))):
    with transaction() as db:
        return _permitted(db, record_id, user)


@router.post("/{record_id}/review")
def review_record(req: MediaReview, record_id: str = Path(pattern=r"^[a-f0-9]{32}$"),
                  user: Principal = Depends(roles("reviewer"))):
    with transaction() as db:
        item = get(db, KIND, record_id)
        if item["owner"] == user.id:
            raise HTTPException(403, "禁止自审")
        if item["payload"]["reviewer_id"] != user.id:
            raise HTTPException(404, "记录不存在")
        if item["status"] != "draft":
            raise HTTPException(409, "记录已审核；本原型不支持修改后重新送审")
        _verify_content(item["payload"])
        payload = {**item["payload"], "review": {"reviewer_id": user.id,
                   "decision": req.decision, "comment": req.comment}}
        return update(db, item, req.expected_version, payload, req.decision,
                      user.id, KIND + ".review." + req.decision)


@router.post("/{record_id}/interpret")
def interpret_record(record_id: str = Path(pattern=r"^[a-f0-9]{32}$"),
                     user: Principal = Depends(roles("editor", "reviewer"))):
    with transaction() as db:
        item = _permitted(db, record_id, user)
        if item["payload"]["kind"] in ("image", "video"):
            raise HTTPException(501, "图像/视频解读未实现；未接入任何AI视觉、OCR或视频处理能力")
        if item["status"] != "approved":
            raise HTTPException(409, "仅可阅读已独立审核通过的文本引文")
        content = _verify_content(item["payload"])
        audit(db, user.id, KIND + ".read_quote", item["id"])
        return {"record_id": item["id"], "version": item["version"],
                "mode": "deterministic-public-quotation", "ai_used": False,
                "original": content["text"], "provenance": content["provenance"],
                "reading_note": "程序仅逐字呈现已审核公开引文和出处；不是AI解释，未推断含义或提供诊疗建议。",
                "note": NOTE}
