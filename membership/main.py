"""Disabled-by-default, local sandbox ledger; no real payments or credentials.

APP_PAYMENT_MODE is disabled|sandbox. Every other value (including live) fails
closed. APP_PAYMENT_SECRET must contain at least 32 characters for sandbox
callbacks. X-Payment-Signature is a lowercase hex HMAC-SHA256 of the exact JSON
body; its timestamp and event_id are inside that signed body. No network calls,
bank card fields, checkout URLs or real membership privileges exist here.

Orders, payment_events and sandbox credits commit in one BEGIN IMMEDIATE SQLite
transaction. Replaying an identical event within the 300-second window returns
idempotently; a new event for an already-paid order is rejected without writes.
No standalone app: the application hub must include this router.
"""
import hashlib
import hmac
import json
import os
import re
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import Field, ValidationError, field_validator

from application_hub.security import Input, Principal, roles
from application_hub.store import audit, create, get, listing, transaction, update

router = APIRouter(prefix="/api/membership", tags=["sandbox-membership"])
ORDER_KIND = "membership_order"
ENTITLEMENT_KIND = "membership_entitlement"
PRODUCT = "sandbox-membership"
AMOUNT_FEN = 100
CURRENCY = "CNY"
NOTE = "本地沙盒账本；未发生真实支付，积分不是正式会员权益，不修改账号角色。"


class OrderCreate(Input):
    product: Literal["sandbox-membership"] = "sandbox-membership"


class PaymentEvent(Input):
    event_id: str = Field(min_length=5, max_length=68, pattern=r"^evt_[A-Za-z0-9_-]{1,64}$")
    timestamp: int = Field(ge=0, le=9999999999)
    event_type: Literal["payment.paid"]
    order_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    amount_fen: int = Field(ge=1, le=1000000)
    currency: Literal["CNY"]

    @field_validator("event_id")
    @classmethod
    def exact_event_id(cls, value):
        if re.fullmatch(r"evt_[A-Za-z0-9_-]{1,64}", value) is None:
            raise ValueError("事件编号必须完整匹配沙盒代号格式")
        return value


def _mode():
    mode = os.environ.get("APP_PAYMENT_MODE", "disabled")
    if mode not in ("disabled", "sandbox"):
        raise HTTPException(503, "真实支付及未知支付模式均不受支持")
    return mode


def _sandbox():
    if _mode() != "sandbox":
        raise HTTPException(503, "支付服务默认关闭；仅可显式启用本地 sandbox")


def _entitlement(db, owner):
    items = listing(db, ENTITLEMENT_KIND, owner=owner, limit=2)
    if len(items) > 1:
        raise HTTPException(503, "沙盒权益账本不一致")
    if items:
        item = items[0]
        payload = item["payload"]
        if (item["status"] != "active" or payload.get("mode") != "sandbox" or
                payload.get("product") != PRODUCT or type(payload.get("credits")) is not int or
                not 1 <= payload["credits"] <= 2000):
            raise HTTPException(503, "沙盒权益账本不一致")
        return item
    return None


@router.get("/me")
def me(user: Principal = Depends(roles("member"))):
    mode = _mode()
    with transaction() as db:
        item = _entitlement(db, user.id)
        return {"user_id": user.id, "role": user.role, "payment_mode": mode,
                "sandbox_credits": item["payload"]["credits"] if item else 0,
                "entitlement_version": item["version"] if item else 0,
                "sandbox_enabled": mode == "sandbox", "real_membership": False, "note": NOTE}


@router.post("/orders", status_code=201)
def create_order(req: OrderCreate, user: Principal = Depends(roles("member"))):
    _sandbox()
    with transaction() as db:
        return create(db, ORDER_KIND, user.id,
                      {"product": PRODUCT, "amount_fen": AMOUNT_FEN, "currency": CURRENCY,
                       "mode": "sandbox", "paid_event_id": None, "note": NOTE}, status="pending")


@router.get("/orders/{order_id}")
def get_order(order_id: str = Path(pattern=r"^[a-f0-9]{32}$"),
              user: Principal = Depends(roles("member"))):
    with transaction() as db:
        item = get(db, ORDER_KIND, order_id)
        if item["owner"] != user.id:
            raise HTTPException(404, "订单不存在")
        return item


def _unique_object(pairs):
    value = {}
    for key, entry in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = entry
    return value


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _matches_order(order, event):
    payload = order["payload"]
    if (payload.get("mode") != "sandbox" or payload.get("product") != PRODUCT or
            type(payload.get("amount_fen")) is not int or
            payload["amount_fen"] != AMOUNT_FEN or payload.get("currency") != CURRENCY or
            event.amount_fen != payload["amount_fen"] or event.currency != payload["currency"]):
        raise HTTPException(409, "订单、金额或币种不匹配")


def _result(event, idempotent):
    return {"event_id": event.event_id, "order_id": event.order_id, "status": "paid",
            "mode": "sandbox", "idempotent": idempotent, "real_payment": False, "note": NOTE}


@router.post("/webhook")
async def webhook(request: Request, signature: str | None = Header(default=None, alias="X-Payment-Signature")):
    _sandbox()
    secret = os.environ.get("APP_PAYMENT_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "沙盒回调签名密钥未安全配置")
    if signature is None or re.fullmatch(r"[a-f0-9]{64}", signature) is None:
        raise HTTPException(401, "回调签名无效")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "仅接收小型JSON沙盒事件")
    # Limit bytes while reading, not after buffering an unbounded request.body().
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4096:
            raise HTTPException(413, "沙盒事件超过4096字节")
        raw.extend(chunk)
    body = bytes(raw)
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "回调签名无效")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
        event = PaymentEvent.model_validate(value)
    except (ValueError, ValidationError, RecursionError):
        # Do not reflect untrusted body fields or credentials in validation errors.
        raise HTTPException(422, "沙盒事件格式无效") from None
    if abs(int(time.time()) - event.timestamp) > 300:
        raise HTTPException(401, "回调时间戳超出300秒窗口")
    digest = hashlib.sha256(body).hexdigest()
    # This transaction serializes concurrent callbacks, including first-time grants.
    with transaction() as db:
        prior = db.execute("SELECT digest FROM payment_events WHERE id=?", (event.event_id,)).fetchone()
        if prior and not hmac.compare_digest(prior["digest"], digest):
            raise HTTPException(409, "事件编号已用于不同内容")
        order = get(db, ORDER_KIND, event.order_id)
        _matches_order(order, event)
        if prior:
            if order["status"] != "paid" or order["payload"].get("paid_event_id") != event.event_id:
                raise HTTPException(409, "事件与订单状态不一致")
            return _result(event, True)
        if order["status"] != "pending":
            raise HTTPException(409, "订单已支付或不允许兑现；不得重复发放权益")
        if not db.execute("SELECT 1 FROM users WHERE id=? AND role='member' AND enabled=1",
                          (order["owner"],)).fetchone():
            raise HTTPException(409, "订单会员账号当前不可用")
        if db.execute("SELECT count(*) FROM payment_events").fetchone()[0] >= 2000:
            raise HTTPException(409, "沙盒事件容量已满")
        entitlement = _entitlement(db, order["owner"])
        credits = entitlement["payload"]["credits"] + 1 if entitlement else 1
        if credits > 2000:
            raise HTTPException(409, "沙盒权益容量已满")
        db.execute("INSERT INTO payment_events(id,digest) VALUES(?,?)", (event.event_id, digest))
        actor = "sandbox-webhook"
        update(db, order, order["version"],
               {**order["payload"], "paid_event_id": event.event_id, "paid_at": event.timestamp},
               "paid", actor, ORDER_KIND + ".sandbox_paid")
        payload = {"product": PRODUCT, "mode": "sandbox", "credits": credits,
                   "last_order_id": order["id"], "note": NOTE}
        if entitlement:
            update(db, entitlement, entitlement["version"], payload, "active", actor,
                   ENTITLEMENT_KIND + ".sandbox_grant")
        else:
            item = create(db, ENTITLEMENT_KIND, order["owner"], payload, status="active")
            audit(db, actor, ENTITLEMENT_KIND + ".sandbox_grant", item["id"])
        audit(db, actor, "payment_event.accepted", event.event_id)
        return _result(event, False)
