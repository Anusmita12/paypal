"""PayPal checkout: create order, capture, and one shared idempotent finalizer.

Every provider call follows the same shape: lock and validate locally, persist the intent (request ID and
exact payload) and commit, call the provider with no transaction open, then record the outcome through
`finalize_from_order`. Timeouts become `unknown` and are resolved by reconciliation, never by guessing.
Capture responses, webhooks and reconciliation all finalize through the same function.
"""

import copy
import secrets
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.approvals import canonical_digest, consume_reservation, release_reservation
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.errors import AppError, not_found
from app.jobs import RetryableJobError, enqueue, handler
from app.models import (
    PURCHASE_TRANSITIONS,
    RESERVATION_TRANSITIONS,
    Capture,
    DemoSession,
    FixtureProviderRecord,
    LedgerEntry,
    Product,
    ProviderOperation,
    Purchase,
    PurchaseEvent,
    Reservation,
    transition,
)
from app.suppliers import utcnow

FIXTURE_MERCHANT_ID = "FIXTUREMERCHANT"
TERMINAL = {"paid", "failed", "cancelled", "manual_review"}


class ProviderTimeout(Exception):
    """The provider may or may not have acted. The outcome must be reconciled, not assumed."""


class ProviderRejected(Exception):
    """A definitive provider answer that the operation did not happen."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code


class PaymentProvider(Protocol):
    mode: str

    def create_order(self, request_id: str, payload: dict) -> dict: ...
    def get_order(self, order_id: str) -> dict: ...
    def capture_order(self, order_id: str, request_id: str) -> dict: ...
    def verify_webhook(self, headers: dict[str, str], event: dict) -> bool: ...
    def create_payout(self, request_id: str, payload: dict) -> dict: ...
    def get_payout(self, payout_batch_id: str) -> dict: ...
    def track_order(self, order_id: str, request_id: str, payload: dict) -> dict: ...
    def get_dispute(self, dispute_id: str) -> dict: ...
    def provide_evidence(self, dispute_id: str, request_id: str, evidences: list[dict]) -> dict: ...


def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


# --- offline fixture provider ---


class FixturePayPal:
    """Offline stand-in with PayPal Orders v2 shapes and request-ID idempotency. Its state commits in its own
    transaction, like a remote system. `faults` injects one-shot failures for recovery tests."""

    mode = "fixture"
    faults: set[str] = set()  # noqa: RUF012 - deliberately shared fault-injection switchboard

    def _get(self, db: Session, kind: str, key: str) -> FixtureProviderRecord | None:
        return db.scalar(
            select(FixtureProviderRecord).where(FixtureProviderRecord.kind == kind, FixtureProviderRecord.key == key)
            .with_for_update()
        )

    def _fault(self, name: str) -> None:
        if name in self.faults:
            self.faults.discard(name)
            raise ProviderTimeout(f"injected fault: {name}")

    def create_order(self, request_id: str, payload: dict) -> dict:
        self._fault("create_timeout_before")
        with get_sessionmaker()() as db:
            prior = self._get(db, "create_request", request_id)
            if prior:
                if prior.data["payload_digest"] != canonical_digest(payload):
                    raise ProviderRejected("DUPLICATE_REQUEST_ID", "Same request ID with a different payload.")
                return copy.deepcopy(self._get(db, "order", prior.data["order_id"]).data)
            order_id = "FX" + secrets.token_hex(8).upper()
            unit = copy.deepcopy(payload["purchase_units"][0])
            unit.setdefault("payee", {"merchant_id": FIXTURE_MERCHANT_ID})
            order = {"id": order_id, "intent": payload["intent"], "status": "CREATED", "purchase_units": [unit]}
            db.add(FixtureProviderRecord(kind="order", key=order_id, data=order))
            db.add(FixtureProviderRecord(kind="create_request", key=request_id,
                                         data={"order_id": order_id, "payload_digest": canonical_digest(payload)}))
            db.commit()
        self._fault("create_timeout_after_commit")
        return copy.deepcopy(order)

    def verify_webhook(self, headers: dict[str, str], event: dict) -> bool:
        """Offline stand-in for PayPal's verification endpoint (tests only)."""
        return headers.get("paypal-transmission-sig") == "fixture-valid"

    def create_payout(self, request_id: str, payload: dict) -> dict:
        self._fault("payout_timeout_before")
        with get_sessionmaker()() as db:
            prior = self._get(db, "payout_request", request_id)
            if prior:
                return copy.deepcopy(self._get(db, "payout", prior.data["batch_id"]).data)
            batch_id = "FXPO" + secrets.token_hex(6).upper()
            batch = {"batch_header": {"payout_batch_id": batch_id, "batch_status": "PENDING",
                                      "sender_batch_header": payload["sender_batch_header"]},
                     "items": copy.deepcopy(payload["items"])}
            db.add(FixtureProviderRecord(kind="payout", key=batch_id, data=batch))
            db.add(FixtureProviderRecord(kind="payout_request", key=request_id, data={"batch_id": batch_id}))
            db.commit()
        self._fault("payout_timeout_after_commit")
        return copy.deepcopy(batch)

    def get_payout(self, payout_batch_id: str) -> dict:
        with get_sessionmaker()() as db:
            rec = self._get(db, "payout", payout_batch_id)
            if not rec:
                raise ProviderRejected("RESOURCE_NOT_FOUND")
            if rec.data["batch_header"]["batch_status"] == "PENDING":  # fixture batches settle on first read
                data = copy.deepcopy(rec.data)
                data["batch_header"]["batch_status"] = "SUCCESS"
                rec.data = data
                db.commit()
            return copy.deepcopy(rec.data)

    def track_order(self, order_id: str, request_id: str, payload: dict) -> dict:
        self._fault("track_timeout_before")
        with get_sessionmaker()() as db:
            if not self._get(db, "order", order_id):
                raise ProviderRejected("RESOURCE_NOT_FOUND")
            prior = self._get(db, "tracker", request_id)
            if not prior:
                db.add(FixtureProviderRecord(kind="tracker", key=request_id, data={"order_id": order_id, **payload}))
                db.commit()
        return {"id": order_id, "status": "COMPLETED"}

    def open_dispute(self, order_id: str, reason: str) -> str:
        """Fixture-only stand-in for a buyer opening a dispute in PayPal's Resolution Center."""
        with get_sessionmaker()() as db:
            order = self._get(db, "order", order_id)
            cap = order.data["purchase_units"][0]["payments"]["captures"][0]
            dispute_id = "PP-D-FX" + secrets.token_hex(4).upper()
            db.add(FixtureProviderRecord(kind="dispute", key=dispute_id, data={
                "dispute_id": dispute_id, "reason": reason, "status": "WAITING_FOR_SELLER_RESPONSE",
                "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": cap["amount"],
                "seller_response_due_date": (utcnow() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "disputed_transactions": [{"seller_transaction_id": cap["id"]}], "evidences": [],
                "links": [{"rel": "provide_evidence", "method": "POST",
                           "href": f"/v1/customer/disputes/{dispute_id}/provide-evidence"}],
            }))
            db.commit()
        return dispute_id

    def get_dispute(self, dispute_id: str) -> dict:
        with get_sessionmaker()() as db:
            rec = self._get(db, "dispute", dispute_id)
            if not rec:
                raise ProviderRejected("RESOURCE_NOT_FOUND")
            return copy.deepcopy(rec.data)

    def provide_evidence(self, dispute_id: str, request_id: str, evidences: list[dict]) -> dict:
        self._fault("evidence_timeout_before")
        with get_sessionmaker()() as db:
            rec = self._get(db, "dispute", dispute_id)
            if not rec:
                raise ProviderRejected("RESOURCE_NOT_FOUND")
            if not self._get(db, "evidence_request", request_id):
                if rec.data["status"] != "WAITING_FOR_SELLER_RESPONSE":
                    raise ProviderRejected("NOT_ELIGIBLE", "Dispute is not waiting for seller evidence.")
                data = copy.deepcopy(rec.data)
                data["evidences"] += [{**e, "source": "SUBMITTED_BY_SELLER"} for e in evidences]
                data["status"], data["links"] = "UNDER_REVIEW", []
                rec.data = data
                db.add(FixtureProviderRecord(kind="evidence_request", key=request_id, data={"dispute_id": dispute_id}))
                db.commit()
        self._fault("evidence_timeout_after_commit")
        return {"links": [{"rel": "self", "href": f"/v1/customer/disputes/{dispute_id}"}]}

    def approve(self, order_id: str) -> dict:
        """Fixture-only stand-in for the buyer approving checkout on PayPal."""
        with get_sessionmaker()() as db:
            rec = self._get(db, "order", order_id)
            if not rec:
                raise ProviderRejected("RESOURCE_NOT_FOUND")
            if rec.data["status"] == "CREATED":
                rec.data = {**rec.data, "status": "APPROVED"}
                db.commit()
            return copy.deepcopy(rec.data)

    def get_order(self, order_id: str) -> dict:
        self._fault("get_timeout")
        with get_sessionmaker()() as db:
            rec = self._get(db, "order", order_id)
            if not rec:
                raise ProviderRejected("RESOURCE_NOT_FOUND")
            return copy.deepcopy(rec.data)

    def capture_order(self, order_id: str, request_id: str) -> dict:
        self._fault("capture_timeout_before")
        with get_sessionmaker()() as db:
            prior = self._get(db, "capture_request", request_id)
            rec = self._get(db, "order", order_id)
            if not rec:
                raise ProviderRejected("RESOURCE_NOT_FOUND")
            if prior:
                return copy.deepcopy(rec.data)
            if rec.data["status"] == "COMPLETED":
                raise ProviderRejected("ORDER_ALREADY_CAPTURED")
            if rec.data["status"] != "APPROVED":
                raise ProviderRejected("ORDER_NOT_APPROVED", "Payer has not yet approved the order.")
            order = copy.deepcopy(rec.data)
            unit = order["purchase_units"][0]
            unit["payments"] = {"captures": [{
                "id": "FXCAP" + secrets.token_hex(6).upper(),
                "status": "COMPLETED",
                "amount": {"currency_code": unit["amount"]["currency_code"], "value": unit["amount"]["value"]},
                "custom_id": unit.get("custom_id"),
                "final_capture": True,
            }]}
            order["status"] = "COMPLETED"
            rec.data = order
            db.add(FixtureProviderRecord(kind="capture_request", key=request_id, data={"order_id": order_id}))
            db.commit()
        self._fault("capture_timeout_after_commit")
        return copy.deepcopy(order)


def get_provider(settings: Settings | None = None) -> PaymentProvider:
    settings = settings or get_settings()
    if settings.payment_mode == "fixture":
        return FixturePayPal()
    from app.paypal_client import SandboxPayPal

    return SandboxPayPal(settings)


def expected_payee(purchase: Purchase, settings: Settings) -> str:
    return FIXTURE_MERCHANT_ID if purchase.payment_mode == "fixture" else settings.paypal_merchant_id


# --- local records ---


def _event(db: Session, purchase: Purchase, kind: str, source: str, summary: str, **data) -> None:
    db.add(PurchaseEvent(purchase_id=purchase.id, kind=kind, source=source, summary=summary, data=data))


def _lock(db: Session, purchase_id: str, session_id: str | None = None) -> Purchase:
    stmt = select(Purchase).where(Purchase.id == purchase_id).with_for_update()
    if session_id is not None:
        stmt = stmt.where(Purchase.session_id == session_id)
    p = db.scalar(stmt)
    if not p:
        raise not_found("purchase")
    return p


def _reservation(db: Session, purchase: Purchase) -> Reservation | None:
    return db.scalar(select(Reservation).where(Reservation.purchase_id == purchase.id).with_for_update())


def _set_status(purchase: Purchase, status: str, reason: str | None = None) -> None:
    transition(purchase, status, PURCHASE_TRANSITIONS, "purchase")
    purchase.status_reason = reason


def _manual_review(db: Session, purchase: Purchase, reason: str, source: str, **data) -> None:
    _set_status(purchase, "manual_review", reason)
    _event(db, purchase, "manual_review", source, f"Stopped for manual review: {reason}", **data)


def order_payload(purchase: Purchase, settings: Settings) -> dict:
    """PayPal Orders v2 body built only from the approved, stored snapshot. Browser amounts never enter."""
    snap = purchase.snapshot
    item_total = sum(line["unit_cents"] * line["qty"] for line in snap["lines"])
    if item_total != snap["subtotal_cents"] or (
        snap["subtotal_cents"] - snap["discount_cents"] + snap["shipping_cents"] + snap["tax_cents"] != purchase.amount_cents
    ):
        raise AppError("snapshot_arithmetic", "Approved snapshot totals do not add up; refusing to create an order.", 409)
    usd = lambda c: {"currency_code": "USD", "value": money(c)}
    unit = {
        "reference_id": purchase.id,
        "custom_id": purchase.id,
        "invoice_id": f"TP-{purchase.id}",
        "description": (f"TradePilot restock from {snap['supplier']['name']}" if purchase.kind == "restock"
                        else "TradePilot prepaid agent tool credit")[:127],
        "amount": {**usd(purchase.amount_cents), "breakdown": {
            "item_total": usd(snap["subtotal_cents"]),
            "shipping": usd(snap["shipping_cents"]),
            "discount": usd(snap["discount_cents"]),
            "tax_total": usd(snap["tax_cents"]),
        }},
        "items": [
            {"name": line["name"][:127], "sku": line["offered_sku"][:127], "quantity": str(line["qty"]),
             "unit_amount": usd(line["unit_cents"]),
             "category": "PHYSICAL_GOODS" if purchase.kind == "restock" else "DIGITAL_GOODS"}
            for line in snap["lines"]
        ],
    }
    if purchase.payment_mode != "fixture":
        unit["payee"] = {"merchant_id": settings.paypal_merchant_id}
    return {"intent": "CAPTURE", "purchase_units": [unit]}


def _operation(db: Session, purchase: Purchase, kind: str, payload: dict) -> ProviderOperation:
    """Persisted intent. A retry reuses the same request ID and identical payload."""
    op = db.scalar(select(ProviderOperation).where(ProviderOperation.purchase_id == purchase.id,
                                                   ProviderOperation.kind == kind).with_for_update())
    if op:
        if op.payload_digest != canonical_digest(payload):
            raise AppError("operation_payload_changed", f"{kind} payload differs from the persisted intent.", 409)
        return op
    op = ProviderOperation(purchase_id=purchase.id, kind=kind, request_id=f"tp-{kind}-{purchase.id}",
                           payload=payload, payload_digest=canonical_digest(payload))
    db.add(op)
    db.flush()
    return op


def _revalidate(db: Session, purchase: Purchase, now: datetime) -> Reservation | None:
    """Before create and before capture: the approved snapshot is intact and (for restocks) its stock is reserved."""
    if canonical_digest(purchase.snapshot) != purchase.digest or purchase.snapshot["total_cents"] != purchase.amount_cents:
        _manual_review(db, purchase, "snapshot_tampered", "system")
        db.commit()
        raise AppError("snapshot_mismatch", "Stored purchase snapshot does not match its approval.", 409)
    if purchase.kind == "wallet_topup":
        return None  # nothing to reserve: credit is created only when the capture is confirmed
    res = _reservation(db, purchase)
    if res is None or res.status != "held":
        raise AppError("reservation_invalid", "Supplier stock is no longer reserved for this purchase.", 409)
    if res.expires_at <= now:
        release_reservation(db, res, "reservation_expired")
        _set_status(purchase, "cancelled", "reservation_expired_before_capture")
        _event(db, purchase, "cancelled", "system", "Reservation expired before payment; stock released.")
        db.commit()
        raise AppError("reservation_expired", "The reservation expired. Run the buyer again for a fresh approval.", 409)
    return res


def _record_unknown(purchase_id: str, kind: str, error: str) -> None:
    with get_sessionmaker()() as db:
        p = _lock(db, purchase_id)
        op = db.scalar(select(ProviderOperation).where(ProviderOperation.purchase_id == p.id, ProviderOperation.kind == kind))
        op.state, op.last_error = "unknown", error[:500]
        if p.status not in TERMINAL:
            _set_status(p, "unknown", f"{kind}_outcome_unknown")
        _event(db, p, "outcome_unknown", "system", f"{kind} outcome unknown ({error[:120]}); reconciliation scheduled.")
        enqueue(db, "reconcile_purchase", {"purchase_id": p.id}, dedupe_key=f"reconcile:{p.id}",
                run_after=utcnow())
        db.commit()


def _record_rejected_before_capture(purchase_id: str, kind: str, code: str) -> None:
    """A definitive failure before any money moved: release stock and fail the purchase."""
    with get_sessionmaker()() as db:
        p = _lock(db, purchase_id)
        op = db.scalar(select(ProviderOperation).where(ProviderOperation.purchase_id == p.id, ProviderOperation.kind == kind))
        op.state, op.last_error = "failed", code
        if p.status not in TERMINAL:
            res = _reservation(db, p)
            if res:
                release_reservation(db, res, code)
            _set_status(p, "failed", f"{kind}_rejected:{code}")
        _event(db, p, "failed", "system", f"PayPal rejected {kind}: {code}. Reserved stock released.")
        db.commit()


# --- owner actions ---


def ensure_order(db: Session, session_id: str, purchase_id: str, provider: PaymentProvider | None = None) -> Purchase:
    """Create (or return) the one PayPal order mapped to this purchase."""
    provider = provider or get_provider()
    settings = get_settings()
    p = _lock(db, purchase_id, session_id)
    if p.paypal_order_id:
        return p
    if p.status not in ("creating", "unknown"):
        raise AppError("order_not_allowed", f"Purchase is {p.status}; no PayPal order can be created.", 409)
    _revalidate(db, p, utcnow())
    op = _operation(db, p, "create_order", order_payload(p, settings))
    op.attempts += 1
    request_id, payload = op.request_id, op.payload
    db.commit()  # intent is durable before the provider is called

    try:
        order = provider.create_order(request_id, payload)
    except ProviderTimeout as exc:
        _record_unknown(purchase_id, "create_order", str(exc))
        raise AppError("payment_status_unknown", "PayPal did not answer in time. We will check the order status.", 502) from exc
    except ProviderRejected as exc:
        _record_rejected_before_capture(purchase_id, "create_order", exc.code)
        raise AppError("paypal_rejected", f"PayPal rejected the order: {exc.code}", 502) from exc
    return _record_order(db, purchase_id, order, "owner")


def _record_order(db: Session, purchase_id: str, order: dict, source: str) -> Purchase:
    p = _lock(db, purchase_id)
    op = db.scalar(select(ProviderOperation).where(ProviderOperation.purchase_id == p.id,
                                                   ProviderOperation.kind == "create_order"))
    op.state, op.response = "succeeded", {"id": order["id"], "status": order["status"]}
    if p.paypal_order_id and p.paypal_order_id != order["id"]:
        _manual_review(db, p, "second_order_for_purchase", source, order_id=order["id"])
    elif not p.paypal_order_id:
        p.paypal_order_id = order["id"]
        if p.status in ("creating", "unknown"):
            _set_status(p, "awaiting_paypal_approval")
        _event(db, p, "order_created", source, f"PayPal order {order['id']} created for {money(p.amount_cents)} USD.",
               order_id=order["id"], request_id=op.request_id)
    db.commit()
    return p


def fixture_buyer_approve(db: Session, session_id: str, purchase_id: str) -> Purchase:
    """Fixture mode only: simulate the buyer approving on PayPal. Real mode uses PayPal's own checkout."""
    if get_settings().payment_mode != "fixture":
        raise AppError("fixture_only", "Simulated buyer approval exists only in fixture payment mode.", 403)
    p = _lock(db, purchase_id, session_id)
    if p.payment_mode != "fixture" or not p.paypal_order_id:
        raise AppError("order_missing", "Create the checkout order first.", 409)
    FixturePayPal().approve(p.paypal_order_id)
    _event(db, p, "buyer_approved", "owner", "Simulated buyer approved the fixture checkout (no real PayPal).")
    db.commit()
    return p


def capture(db: Session, session_id: str, purchase_id: str, provider: PaymentProvider | None = None) -> Purchase:
    """Capture the buyer-approved mapped order once. Repeat calls reuse the same capture request ID."""
    provider = provider or get_provider()
    p = _lock(db, purchase_id, session_id)
    if p.status == "paid":
        return p
    if not p.paypal_order_id:
        raise AppError("order_missing", "No PayPal order exists for this purchase yet.", 409)
    if p.status not in ("awaiting_paypal_approval", "capture_pending", "unknown"):
        raise AppError("capture_not_allowed", f"Purchase is {p.status}; it cannot be captured.", 409)
    order_id = p.paypal_order_id
    db.commit()

    try:
        order = provider.get_order(order_id)
    except ProviderTimeout as exc:
        raise AppError("provider_unavailable", "PayPal is not responding; try again shortly.", 503) from exc
    if order["status"] == "COMPLETED":
        return finalize_from_order(db, purchase_id, order, "reconciliation")
    if order["status"] != "APPROVED":
        raise AppError("order_not_approved", "The buyer has not approved this PayPal checkout yet.", 409)

    p = _lock(db, purchase_id, session_id)
    if p.status == "paid":
        return p
    if p.status == "awaiting_paypal_approval":
        res = _revalidate(db, p, utcnow())
        if order["purchase_units"][0]["amount"]["value"] != money(p.amount_cents):
            _manual_review(db, p, "order_amount_mismatch", "owner", order_value=order["purchase_units"][0]["amount"])
            db.commit()
            raise AppError("order_mismatch", "The PayPal order does not match the approved total.", 409)
        # Pin stock: from here on an unknown outcome must not let TTL cleanup release it.
        if res is not None:
            transition(res, "capture_in_progress", RESERVATION_TRANSITIONS, "reservation")
        _set_status(p, "capture_pending")
        _event(db, p, "buyer_approved", "owner", "PayPal reports the buyer approved; capture started.")
    op = _operation(db, p, "capture_order", {"order_id": order_id})
    op.attempts += 1
    request_id = op.request_id
    db.commit()

    try:
        result = provider.capture_order(order_id, request_id)
    except ProviderTimeout as exc:
        _record_unknown(purchase_id, "capture_order", str(exc))
        raise AppError("payment_status_unknown", "PayPal did not confirm the capture in time. Checking status.", 502) from exc
    except ProviderRejected as exc:
        if exc.code == "ORDER_ALREADY_CAPTURED":
            return finalize_from_order(db, purchase_id, provider.get_order(order_id), "reconciliation")
        _record_rejected_before_capture(purchase_id, "capture_order", exc.code)
        raise AppError("paypal_rejected", f"PayPal declined the capture: {exc.code}", 502) from exc
    return finalize_from_order(db, purchase_id, result, "capture_response")


# --- the shared finalizer ---


def finalize_from_order(db: Session, purchase_id: str, order: dict, source: str) -> Purchase:
    """Apply trusted provider evidence exactly once. Used by capture responses, webhooks and reconciliation."""
    settings = get_settings()
    p = _lock(db, purchase_id)
    if p.status in ("paid", "manual_review"):
        db.commit()
        return p
    unit = (order.get("purchase_units") or [{}])[0]
    captures = (unit.get("payments") or {}).get("captures") or []
    correlation = {unit.get("custom_id"), unit.get("reference_id"), *(c.get("custom_id") for c in captures)}
    if order.get("id") != p.paypal_order_id or p.id not in correlation:
        _manual_review(db, p, "order_correlation_mismatch", source, order_id=order.get("id"))
        db.commit()
        return p
    if not captures:
        db.commit()
        return p
    cap = captures[0]
    op = db.scalar(select(ProviderOperation).where(ProviderOperation.purchase_id == p.id,
                                                   ProviderOperation.kind == "capture_order"))
    if cap["status"] == "COMPLETED":
        payee = (unit.get("payee") or {}).get("merchant_id")
        problems = []
        if cap["amount"]["currency_code"] != "USD" or cap["amount"]["value"] != money(p.amount_cents):
            problems.append("capture_amount_mismatch")
        if payee != expected_payee(p, settings):
            problems.append("payee_mismatch")
        if p.status in ("failed", "cancelled"):
            problems.append(f"captured_after_{p.status}")
        if problems:
            _manual_review(db, p, problems[0], source, capture_id=cap["id"], amount=cap["amount"], payee=payee)
            db.commit()
            return p
        if op:
            op.state, op.response = "succeeded", {"id": order["id"], "status": order["status"], "capture_id": cap["id"]}
        db.execute(insert(Capture).values(
            id=cap["id"], purchase_id=p.id, order_id=order["id"], status=cap["status"], amount_cents=p.amount_cents,
            currency="USD", payee_merchant_id=payee, source=source,
        ).on_conflict_do_nothing())
        db.execute(insert(LedgerEntry).values(
            session_id=p.session_id, business_key=f"purchase_paid:{p.id}",
            kind="purchase_paid" if p.kind == "restock" else "wallet_topup",
            amount_cents=p.amount_cents, purchase_id=p.id,
            details={"order_id": order["id"], "capture_id": cap["id"], "supplier": p.snapshot.get("supplier", {}).get("id")},
        ).on_conflict_do_nothing())
        if p.kind == "wallet_topup":
            # Runs once: the locked purchase moves to paid in this same transaction.
            db.execute(update(DemoSession).where(DemoSession.id == p.session_id)
                       .values(wallet_balance_cents=DemoSession.wallet_balance_cents + p.amount_cents))
        if p.kind == "restock":
            res = _reservation(db, p)
            consume_reservation(db, res)
            for line in p.snapshot["lines"]:
                # Paid goods are on their way: incoming rises once. Receiving them is a separate action.
                db.execute(update(Product).where(Product.session_id == p.session_id, Product.sku == line["sku"])
                           .values(incoming=Product.incoming + line["qty"]))
        if p.status == "awaiting_paypal_approval":
            _set_status(p, "capture_pending")
        _set_status(p, "paid")
        _event(db, p, "paid", source, f"Capture {cap['id']} completed for {money(p.amount_cents)} USD.",
               capture_id=cap["id"], order_id=order["id"])
    elif cap["status"] == "PENDING":
        if p.status != "capture_pending":
            _set_status(p, "capture_pending", "capture_pending_at_paypal")
        reason = (cap.get("status_details") or {}).get("reason", "unspecified")
        _event(db, p, "capture_pending", source, f"PayPal holds capture {cap['id']} as pending ({reason}).")
    elif cap["status"] in ("DECLINED", "FAILED"):
        res = _reservation(db, p)
        if res:
            release_reservation(db, res, "capture_declined")
        _set_status(p, "failed", f"capture_{cap['status'].lower()}")
        _event(db, p, "failed", source, f"Capture {cap['status'].lower()}; reserved stock released.")
    db.commit()
    return p


# --- reconciliation ---


def reconcile(db: Session, purchase_id: str, provider: PaymentProvider | None = None, source: str = "reconciliation") -> Purchase:
    """Ask the provider what actually happened and converge. Retries reuse the persisted request IDs."""
    provider = provider or get_provider()
    p = _lock(db, purchase_id)
    if p.status in TERMINAL:
        db.commit()
        return p
    order_id = p.paypal_order_id
    create_op = db.scalar(select(ProviderOperation).where(ProviderOperation.purchase_id == p.id,
                                                          ProviderOperation.kind == "create_order"))
    capture_op = db.scalar(select(ProviderOperation).where(ProviderOperation.purchase_id == p.id,
                                                           ProviderOperation.kind == "capture_order"))
    _event(db, p, "reconcile", source, f"Checking PayPal status (local status {p.status}).")
    db.commit()

    if not order_id:
        if not create_op or create_op.state not in ("pending", "unknown"):
            return p
        # The create response was lost before we learned the order ID: replay the same request ID and payload.
        order = provider.create_order(create_op.request_id, create_op.payload)
        return _record_order(db, purchase_id, order, source)

    order = provider.get_order(order_id)
    if (order.get("purchase_units") or [{}])[0].get("payments", {}).get("captures"):
        return finalize_from_order(db, purchase_id, order, source)
    p = _lock(db, purchase_id)
    if order["status"] == "APPROVED" and capture_op and capture_op.state in ("pending", "unknown"):
        db.commit()
        result = provider.capture_order(order_id, capture_op.request_id)
        return finalize_from_order(db, purchase_id, result, source)
    if p.status == "unknown" and order["status"] in ("CREATED", "APPROVED", "PAYER_ACTION_REQUIRED") and not capture_op:
        _set_status(p, "awaiting_paypal_approval", None)
        _event(db, p, "recovered", source, f"Order {order_id} exists at PayPal ({order['status']}); awaiting buyer.")
    db.commit()
    return p


@handler("reconcile_purchase")
def reconcile_job(db: Session, payload: dict) -> None:
    try:
        reconcile(db, payload["purchase_id"])
    except ProviderTimeout as exc:
        raise RetryableJobError(str(exc)) from exc
