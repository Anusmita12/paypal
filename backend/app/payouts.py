"""Settle accrued pay-per-call earnings to tool providers with PayPal Payouts.

Claim first (usage rows get the batch ID in the same transaction that creates the batch), then call PayPal with a
stable sender_batch_id and PayPal-Request-Id. A lost response leaves the batch `unknown`; retrying reuses the same
IDs, so a provider can never be paid twice for the same calls.
"""

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError, not_found
from app.models import LedgerEntry, PaidTool, PayoutBatch, ToolUsage
from app.payments import ProviderRejected, ProviderTimeout, get_provider, money

MIN_PAYOUT_CENTS = 1
FINAL = {"SUCCESS": "success", "DENIED": "failed", "CANCELED": "failed"}


def _receiver(provider_id: str) -> str:
    settings = get_settings()
    if settings.payment_mode == "fixture":
        return f"{provider_id}@providers.example.test"
    if not settings.paypal_payout_receiver:
        raise AppError("payout_receiver_missing", "Set PAYPAL_PAYOUT_RECEIVER to a Sandbox personal account email.", 409)
    return settings.paypal_payout_receiver


def payload(batch: PayoutBatch, provider_name: str) -> dict:
    return {
        "sender_batch_header": {
            "sender_batch_id": batch.sender_batch_id,
            "email_subject": "TradePilot tool earnings",
            "email_message": f"Settlement for {batch.item_count} paid agent tool calls.",
        },
        "items": [{
            "recipient_type": "EMAIL",
            "amount": {"value": money(batch.amount_cents), "currency": "USD"},
            "receiver": batch.receiver,
            "note": f"{provider_name}: {batch.item_count} tool calls",
            "sender_item_id": f"{batch.id}-1",
        }],
    }


def claim(db: Session, session_id: str) -> list[PayoutBatch]:
    """Create one batch per provider for all unsettled usage, atomically claiming those rows."""
    rows = db.scalars(select(ToolUsage).where(ToolUsage.session_id == session_id, ToolUsage.payout_batch_id.is_(None))
                      .with_for_update(skip_locked=True)).all()
    by_provider: dict[str, list[ToolUsage]] = {}
    for u in rows:
        by_provider.setdefault(u.provider_id, []).append(u)
    batches = []
    for provider_id, usages in sorted(by_provider.items()):
        amount = sum(u.price_cents for u in usages)
        if amount < MIN_PAYOUT_CENTS:
            continue
        batch = PayoutBatch(session_id=session_id, provider_id=provider_id, amount_cents=amount, item_count=len(usages),
                            receiver=_receiver(provider_id), sender_batch_id="", payment_mode=get_settings().payment_mode)
        db.add(batch)
        db.flush()
        batch.sender_batch_id = f"tp-payout-{batch.id}"
        db.execute(update(ToolUsage).where(ToolUsage.id.in_([u.id for u in usages])).values(payout_batch_id=batch.id))
        batches.append(batch)
    return batches


def submit(db: Session, batch_id: str) -> PayoutBatch:
    batch = db.scalar(select(PayoutBatch).where(PayoutBatch.id == batch_id).with_for_update())
    if batch.status not in ("pending", "unknown"):
        db.commit()
        return batch
    name = db.scalar(select(PaidTool.provider_name).where(PaidTool.provider_id == batch.provider_id).limit(1)) or batch.provider_id
    body = payload(batch, name)
    batch.attempts += 1
    db.commit()  # intent persisted; no lock held during the provider call
    try:
        result = get_provider().create_payout(batch.sender_batch_id, body)
    except ProviderTimeout as exc:
        batch = db.get(PayoutBatch, batch_id)
        batch.status, batch.last_error = "unknown", str(exc)[:300]
        db.commit()
        return batch
    except ProviderRejected as exc:
        batch = db.get(PayoutBatch, batch_id)
        # A duplicate means an earlier attempt reached PayPal; never resubmit with new IDs.
        dup = "DUPLICATE" in exc.code or "ALREADY" in exc.code
        batch.status, batch.last_error = ("manual_review" if dup else "failed"), exc.code
        if not dup:
            db.execute(update(ToolUsage).where(ToolUsage.payout_batch_id == batch.id).values(payout_batch_id=None))
        db.commit()
        return batch
    return _record(db, batch_id, result)


def _record(db: Session, batch_id: str, result: dict) -> PayoutBatch:
    batch = db.scalar(select(PayoutBatch).where(PayoutBatch.id == batch_id).with_for_update())
    header = result.get("batch_header") or {}
    if header.get("sender_batch_header", {}).get("sender_batch_id", batch.sender_batch_id) != batch.sender_batch_id:
        batch.status, batch.last_error = "manual_review", "sender_batch_id mismatch"
        db.commit()
        return batch
    batch.paypal_batch_id = batch.paypal_batch_id or header.get("payout_batch_id")
    batch.paypal_status = header.get("batch_status")
    batch.status = FINAL.get(batch.paypal_status or "", "submitted")
    if batch.status == "success":
        db.execute(insert_ledger(batch))
    db.commit()
    return batch


def insert_ledger(batch: PayoutBatch):
    from sqlalchemy.dialects.postgresql import insert

    return insert(LedgerEntry).values(
        session_id=batch.session_id, business_key=f"payout:{batch.id}", kind="provider_payout",
        amount_cents=-batch.amount_cents,
        details={"provider": batch.provider_id, "paypal_batch_id": batch.paypal_batch_id, "items": batch.item_count},
    ).on_conflict_do_nothing()


def refresh(db: Session, session_id: str, batch_id: str) -> PayoutBatch:
    batch = db.scalar(select(PayoutBatch).where(PayoutBatch.id == batch_id, PayoutBatch.session_id == session_id))
    if not batch:
        raise not_found("payout")
    if batch.status == "unknown" or (batch.status == "pending" and not batch.paypal_batch_id):
        return submit(db, batch.id)  # same sender_batch_id + request ID: idempotent replay
    if batch.status != "submitted" or not batch.paypal_batch_id:
        return batch
    try:
        result = get_provider().get_payout(batch.paypal_batch_id)
    except ProviderTimeout as exc:
        raise AppError("provider_unavailable", "PayPal is not responding; try again shortly.", 503) from exc
    return _record(db, batch.id, result)


def settle(db: Session, session_id: str) -> list[PayoutBatch]:
    batches = claim(db, session_id)
    db.commit()
    if not batches:
        raise AppError("nothing_to_settle", "No unsettled tool earnings.", 409)
    return [submit(db, b.id) for b in batches]


def view(b: PayoutBatch) -> dict:
    return {"payout_id": b.id, "provider_id": b.provider_id, "amount_cents": b.amount_cents, "item_count": b.item_count,
            "receiver": b.receiver, "sender_batch_id": b.sender_batch_id, "status": b.status,
            "paypal_batch_id": b.paypal_batch_id, "paypal_status": b.paypal_status, "payment_mode": b.payment_mode,
            "attempts": b.attempts, "last_error": b.last_error, "created_at": b.created_at.isoformat()}


def batches_for(db: Session, session_id: str) -> list[dict]:
    rows = db.scalars(select(PayoutBatch).where(PayoutBatch.session_id == session_id).order_by(PayoutBatch.created_at.desc()))
    return [view(b) for b in rows]


def unsettled_cents(db: Session, session_id: str) -> int:
    return db.scalar(select(func.coalesce(func.sum(ToolUsage.price_cents), 0)).where(
        ToolUsage.session_id == session_id, ToolUsage.payout_batch_id.is_(None)))
