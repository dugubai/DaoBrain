"""Person-bound SQLite card workflow; only exact GREEN classics may publish."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from application_hub.security import Input, Principal, Review, roles
from application_hub.store import create, get, listing, transaction, update
from common.schemas import Provenance
from knowledge_library.main import approved_evidence, publish_card, withdraw_card

router = APIRouter()
Text = Annotated[str, Field(min_length=1, max_length=4000)]
Annotation = Annotated[str, Field(max_length=2000)]
CardZone = Literal["green", "red", "unknown"]


class CardProvenance(Provenance):
    # The shared provenance model alone ignores unknown fields and coerces types.
    # Literal strings keep HTTP JSON strict without requiring Python Enum objects.
    # Provided provenance must be complete; do not infer omitted source metadata.
    model_config = ConfigDict(extra="forbid", strict=True)
    locator: str = Field(max_length=512)
    source_type: Literal["public_classic", "faq", "approved", "original"]
    quote: str = Field(max_length=4000)
    version: str = Field(max_length=64)
    content_hash: str = Field(max_length=64)


class CardContent(Input):
    text: Text
    annotation: Annotation = ""
    zone: CardZone = "unknown"  # A missing label never authorizes public release.
    provenance: CardProvenance | None = None

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value):
        if not value.strip():
            raise ValueError("字卡正文不能为空白")
        # Do not strip or normalize canonical text: approval compares exact bytes.
        return value


class Revision(Input):
    expected_version: int = Field(ge=1, le=10000)
    text: Text | None = None
    annotation: Annotation | None = None
    zone: CardZone | None = None
    provenance: CardProvenance | None = None

    @model_validator(mode="after")
    def nonnull_content_fields(self):
        for name in ("text", "annotation", "zone"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError("修订字段不能显式为空值")
        return self


_readers = roles("member", "editor", "reviewer", "admin")
_editors = roles("editor")
_reviewers = roles("reviewer", "admin")


def _visible(card, user):
    if user.role not in ("reviewer", "admin") and card["owner"] != user.id:
        raise HTTPException(403, "只能查看自己的字卡")


def _version(card, expected):
    if card["version"] != expected or expected >= 10000:
        raise HTTPException(409, "记录版本冲突或达到原型版本上限")


@router.post("/api/cards", status_code=201)
def create_card(req: CardContent, user: Principal = Depends(_editors)):
    payload = req.model_dump(mode="json")
    payload.update(updated_by=user.id, reviewed_by=None, review_comment="")
    with transaction() as db:
        return create(db, "card", user.id, payload)


@router.get("/api/cards")
def list_cards(offset: int = Query(default=0, ge=0, le=2000),
               limit: int = Query(default=20, ge=1, le=50),
               user: Principal = Depends(_readers)):
    owner = None if user.role in ("reviewer", "admin") else user.id
    with transaction() as db:
        cards = listing(db, "card", owner=owner, offset=offset, limit=limit)
        if owner is None:
            total = db.execute("SELECT count(*) FROM objects WHERE kind='card'").fetchone()[0]
        else:
            total = db.execute("SELECT count(*) FROM objects WHERE kind='card' AND owner=?",
                               (owner,)).fetchone()[0]
        return {"cards": cards, "total": total, "offset": offset, "limit": limit}


@router.get("/api/cards/{card_id}")
def read_card(card_id: str, user: Principal = Depends(_readers)):
    with transaction() as db:
        card = get(db, "card", card_id)
        _visible(card, user)
        return card


@router.patch("/api/cards/{card_id}")
def revise_card(card_id: str, req: Revision, user: Principal = Depends(_editors)):
    changes = req.model_dump(mode="json", exclude_unset=True, exclude={"expected_version"})
    if not changes:
        raise HTTPException(422, "修订须包含正文、批注或出处字段")
    with transaction() as db:
        card = get(db, "card", card_id)
        if card["owner"] != user.id:
            raise HTTPException(403, "只有提交者可以修订字卡")
        _version(card, req.expected_version)
        if "zone" in changes and changes["zone"] != card["payload"]["zone"]:
            raise HTTPException(403, "修订不能重新标记原始分区")
        content = {key: card["payload"][key]
                   for key in ("text", "annotation", "zone", "provenance")}
        try:
            payload = CardContent.model_validate({**content, **changes}).model_dump(mode="json")
        except ValidationError:
            raise HTTPException(422, "修订内容不符合严格字卡契约") from None
        payload.update(updated_by=user.id, reviewed_by=None, review_comment="")
        revised = update(db, card, req.expected_version, payload, "draft", user.id, "card.revise")
        # Keep the old approved reference and its evidence; never delete it.
        # Failure to withdraw also rolls back the card revision and its audit row.
        withdraw_card(db, card, user.id)
        return revised


@router.post("/api/cards/{card_id}/review")
def review_card(card_id: str, req: Review, user: Principal = Depends(_reviewers)):
    with transaction() as db:
        card = get(db, "card", card_id)
        if user.id in (card["owner"], card["payload"].get("updated_by")):
            raise HTTPException(403, "不能审核自己提交或修改的字卡")
        _version(card, req.expected_version)
        if card["status"] != "draft":
            raise HTTPException(409, "该版本已审核，须先修订为草稿")
        evidence = approved_evidence(card["payload"]) if req.decision == "approved" else None
        payload = {**card["payload"], "reviewed_by": user.id, "review_comment": req.comment}
        reviewed = update(db, card, req.expected_version, payload, req.decision,
                          user.id, "card.review." + req.decision)
        if evidence is not None:
            publish_card(db, reviewed, evidence, user.id)
        else:
            withdraw_card(db, card, user.id)
        return reviewed
