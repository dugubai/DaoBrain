"""Bounded, single-process card simulation. State is lost on process restart.

The shared API/internal keys distinguish simulated caller/reviewer roles, not
production person identities. Approval never publishes cards to a corpus.
"""
from threading import Lock
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.health import health_router
from common.schemas import Provenance, Retrieved, Zone
from common.security import require_api, require_internal

app = FastAPI(title="DaoBrain card_annotator · in-memory simulation")
app.include_router(health_router("card_annotator"))
MAX_CARDS = 200
_cards: dict[str, "Card"] = {}
_lock = Lock()


class CardContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    annotation: str = Field(default="", max_length=2000)
    zone: Zone = Zone.UNKNOWN
    provenance: Provenance | None = None

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Card text must not be blank")
        return value

    @field_validator("provenance")
    @classmethod
    def bounded_provenance(cls, value: Provenance | None) -> Provenance | None:
        if value and (len(value.quote) > 4000 or len(value.locator) > 512
                      or len(value.version) > 64 or len(value.content_hash) > 64):
            raise ValueError("Source provenance exceeds simulation limits")
        return value


class RevisionRequest(CardContent):
    expected_version: int = Field(ge=1, le=10000, strict=True)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_version: int = Field(ge=1, le=10000, strict=True)
    decision: Literal["approved", "rejected"]
    target_zone: Zone = Zone.GREEN
    comment: str = Field(default="", max_length=2000)


class Card(CardContent):
    card_id: str
    version: int = 1
    status: Literal["draft", "approved", "rejected", "revision"] = "draft"
    created_by: str
    updated_by: str
    reviewed_by: str | None = None
    review_comment: str = ""
    simulated: bool = True
    storage: str = "single-process in-memory; no persistence or corpus publication"


def reviewer_identity(_: None = Depends(require_internal)) -> str:
    # An identity is derived from the authenticated role, never a body/header name.
    return "simulation-reviewer"


def _get_card(card_id: str) -> Card:
    card = _cards.get(card_id)
    if card is None:
        raise HTTPException(404, "Card not found")
    return card


def _check_version(card: Card, expected: int) -> None:
    if card.version != expected:
        raise HTTPException(409, "Version conflict; fetch the current card before retrying")
    if card.version >= 10000:
        raise HTTPException(409, "Simulation revision limit reached")


@app.post("/cards", response_model=Card, status_code=201)
def create_card(req: CardContent, caller: str = Depends(require_api)):
    with _lock:
        if len(_cards) >= MAX_CARDS:
            raise HTTPException(409, "In-memory simulation card capacity reached")
        card = Card(**req.model_dump(), card_id=uuid4().hex,
                    created_by=caller, updated_by=caller)
        _cards[card.card_id] = card
        return card.model_copy(deep=True)


@app.get("/cards", dependencies=[Depends(require_api)])
def list_cards(offset: int = Query(default=0, ge=0, le=MAX_CARDS),
               limit: int = Query(default=20, ge=1, le=50)):
    with _lock:
        items = list(_cards.values())
        return {"cards": [item.model_copy(deep=True) for item in items[offset:offset + limit]],
                "total": len(items), "simulated": True,
                "storage": "single-process in-memory; state lost on restart"}


@app.patch("/cards/{card_id}", response_model=Card)
def revise_card(card_id: str, req: RevisionRequest, caller: str = Depends(require_api)):
    with _lock:
        card = _get_card(card_id)
        if caller != card.created_by:
            raise HTTPException(403, "Only the authenticated draft owner can revise")
        _check_version(card, req.expected_version)
        if card.status not in ("approved", "rejected"):
            raise HTTPException(409, "Only reviewed cards can enter revision")
        if req.zone != card.zone:
            raise HTTPException(403, "Revision cannot relabel a card's original zone")
        changes = req.model_dump(exclude={"expected_version"})
        updated = Card(**{**card.model_dump(), **changes, "version": card.version + 1,
                          "status": "revision", "updated_by": caller,
                          "reviewed_by": None, "review_comment": ""})
        _cards[card_id] = updated
        return updated.model_copy(deep=True)


@app.post("/review", response_model=Card)
def review_card(req: ReviewRequest, reviewer: str = Depends(reviewer_identity)):
    with _lock:
        card = _get_card(req.card_id)
        if reviewer in (card.created_by, card.updated_by):
            raise HTTPException(403, "Self-review is not permitted")
        _check_version(card, req.expected_version)
        if card.status not in ("draft", "revision"):
            raise HTTPException(409, "Card has already been reviewed; create a revision first")
        provenance = card.provenance
        if (provenance is None or not provenance.source_id.strip()
                or not provenance.title.strip() or not provenance.quote.strip()
                or not provenance.version.strip() or len(provenance.content_hash) != 64
                or any(char not in "0123456789abcdef" for char in provenance.content_hash)):
            raise HTTPException(422, "Review requires source, quote, exact version and SHA-256 provenance")
        if req.decision == "approved":
            if card.zone != Zone.GREEN or req.target_zone != Zone.GREEN:
                raise HTTPException(403, "Only trusted GREEN sample cards can be approved; RED/UNKNOWN stay blocked")
            # Content and every provenance field must match the canonical sample.
            from common.catalog import trusted
            evidence = Retrieved(text=card.text, provenance=provenance, zone=card.zone)
            if not trusted(evidence):
                raise HTTPException(403, "Approval requires an exact trusted GREEN sample and provenance")
            if card.annotation and card.annotation not in (card.text, provenance.quote):
                raise HTTPException(403, "Simulation cannot approve unverified annotation prose; use an empty annotation or exact trusted sample quotation")
        updated = card.model_copy(update={"status": req.decision, "version": card.version + 1,
                                          "reviewed_by": reviewer,
                                          "review_comment": req.comment}, deep=True)
        _cards[card.card_id] = updated
        return updated.model_copy(deep=True)
