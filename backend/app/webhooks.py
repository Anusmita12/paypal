"""Verified PayPal webhook inbox. Verify, store durably, acknowledge; a job then re-reads the order from PayPal and
finalizes through the same function as capture responses and reconciliation. Payload contents are never trusted
as proof of payment on their own."""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.errors import AppError
from app.jobs import RetryableJobError, enqueue, handler
from app.models import Purchase, PurchaseEvent, WebhookEvent
from app.payments import ProviderTimeout, finalize_from_order, get_provider

CAPTURE_EVENTS = {"PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.PENDING", "PAYMENT.CAPTURE.DENIED",
                  "PAYMENT.CAPTURE.DECLINED"}
ORDER_EVENTS = {"CHECKOUT.ORDER.APPROVED", "CHECKOUT.ORDER.COMPLETED"}
HEADERS = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig",
           "paypal-transmission-time")


def receive(db: Session, headers: dict[str, str], event: dict) -> dict:
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("event_type"), str):
        raise AppError("invalid_webhook", "Not a PayPal webhook event.", 400)
    try:
        verified = get_provider().verify_webhook({k: headers.get(k, "") for k in HEADERS}, event)
    except ProviderTimeout as exc:
        raise AppError("verification_unavailable", "Could not verify the signature right now; retry.", 503) from exc
    if not verified:
        raise AppError("invalid_signature", "Webhook signature verification failed.", 400)

    resource = event.get("resource") or {}
    inserted = db.execute(
        insert(WebhookEvent)
        .values(provider_event_id=event["id"][:80], event_type=event["event_type"][:80],
                resource_id=str(resource.get("id", ""))[:80] or None, payload=event)
        .on_conflict_do_nothing(index_elements=["provider_event_id"])
        .returning(WebhookEvent.id)
    ).scalar()
    if inserted is None:
        db.execute(update(WebhookEvent).where(WebhookEvent.provider_event_id == event["id"][:80])
                   .values(delivery_count=WebhookEvent.delivery_count + 1))
        db.commit()
        return {"status": "duplicate"}
    enqueue(db, "process_webhook", {"webhook_event_id": inserted}, dedupe_key=f"webhook:{inserted}")
    db.commit()  # stored before we acknowledge
    return {"status": "accepted"}


def _order_id(event: dict) -> str | None:
    resource = event.get("resource") or {}
    if event["event_type"] in ORDER_EVENTS:
        return resource.get("id")
    return ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")


@handler("process_webhook")
def process(db: Session, payload: dict) -> None:
    ev = db.scalar(select(WebhookEvent).where(WebhookEvent.id == payload["webhook_event_id"]).with_for_update())
    if ev is None or ev.status in ("processed", "ignored"):
        return
    event = ev.payload
    order_id = _order_id(event)
    purchase = db.scalar(select(Purchase).where(Purchase.paypal_order_id == order_id)) if order_id else None
    if event["event_type"] not in CAPTURE_EVENTS | ORDER_EVENTS or purchase is None:
        ev.status, ev.last_error = "ignored", None if purchase or not order_id else "no matching purchase"
        ev.processed_at = datetime.now(UTC)
        db.commit()
        return
    purchase_id = purchase.id
    db.add(PurchaseEvent(purchase_id=purchase_id, kind="webhook", source="webhook",
                         summary=f"Verified webhook {event['event_type']} ({event['id'][:24]}).", data={}))
    db.commit()
    try:
        order = get_provider().get_order(order_id)  # authoritative state, not the payload
    except ProviderTimeout as exc:
        raise RetryableJobError(str(exc)) from exc
    finalize_from_order(db, purchase_id, order, "webhook")
    ev = db.get(WebhookEvent, payload["webhook_event_id"])
    ev.status, ev.processed_at = "processed", datetime.now(UTC)
    db.commit()
