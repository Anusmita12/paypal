"""Immutable proposals, exact-snapshot owner approval, and atomic supplier-stock reservations."""

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.compare import compare, evaluate
from app.config import get_settings
from app.errors import AppError, not_found
from app.models import (
    PROPOSAL_TRANSITIONS,
    PURCHASE_TRANSITIONS,
    RESERVATION_TRANSITIONS,
    Approval,
    DemoSession,
    Procurement,
    Proposal,
    Purchase,
    Quote,
    Reservation,
    Supplier,
    transition,
)
from app.procurement import set_procurement_status
from app.suppliers import quote_staleness, utcnow

TAX_NOTE = "Synthetic scenario: tax excluded."
PAYEE_NOTE = "Paid to one demo storefront (PayPal Sandbox business account); suppliers are simulated offer sources."


def canonical_digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_snapshot(procurement: Procurement, quote: Quote, supplier: Supplier, version: int) -> dict:
    return {
        "procurement_id": procurement.id,
        "proposal_version": version,
        "quote_id": quote.id,
        "supplier": {"id": supplier.code, "name": supplier.name, "revision": quote.supplier_revision},
        "lines": [
            {
                "sku": line["sku"],
                "offered_sku": line["offered_sku"],
                "name": line["name"],
                "attributes": line["attributes"],
                "qty": line["qty"],
                "unit_cents": line["unit_cents"],
                "line_cents": line["line_cents"],
                "is_surplus": line["is_surplus"],
            }
            for line in quote.lines
        ],
        "subtotal_cents": quote.subtotal_cents,
        "discount_bps": quote.discount_bps,
        "discount_cents": quote.discount_cents,
        "shipping_cents": quote.shipping_cents,
        "tax_cents": quote.tax_cents,
        "tax_note": TAX_NOTE,
        "total_cents": quote.total_cents,
        "currency": quote.currency,
        "delivery_date": quote.delivery_date.isoformat(),
        "deadline": procurement.requirements["deadline"],
        "budget_cents": procurement.requirements["budget_cents"],
        "quote_expires_at": quote.expires_at.isoformat(),
        "payee_note": PAYEE_NOTE,
    }


def create_proposal(
    db: Session, procurement: Procurement, quote_id: str, run_id: str | None, explanation: str, now: datetime | None = None
) -> Proposal:
    now = now or utcnow()
    quote = db.get(Quote, quote_id)
    if not quote or quote.procurement_id != procurement.id:
        raise not_found("quote")
    result = compare(db, procurement, now)
    if result["best_quote_id"] is None:
        raise AppError("no_feasible_offer", "No quote satisfies every constraint; nothing can be proposed.", 409)
    if result["best_quote_id"] != quote.id:
        raise AppError(
            "not_best_feasible_offer",
            "Only the lowest-total feasible quote can be proposed.",
            409,
            {"best_quote_id": result["best_quote_id"]},
        )
    supplier = db.get(Supplier, quote.supplier_id)
    for prev in db.scalars(select(Proposal).where(Proposal.procurement_id == procurement.id, Proposal.status == "current")):
        transition(prev, "superseded", PROPOSAL_TRANSITIONS, "proposal")
        prev.status_reason = "newer_proposal"
    version = (db.scalar(select(func.max(Proposal.version)).where(Proposal.procurement_id == procurement.id)) or 0) + 1
    snapshot = build_snapshot(procurement, quote, supplier, version)
    proposal = Proposal(
        procurement_id=procurement.id,
        session_id=procurement.session_id,
        run_id=run_id,
        version=version,
        quote_id=quote.id,
        snapshot=snapshot,
        digest=canonical_digest(snapshot),
        comparison=result,
        explanation=explanation[:2000],
    )
    db.add(proposal)
    set_procurement_status(procurement, "awaiting_approval", None)
    db.flush()
    return proposal


def _invalidate(db: Session, proposal: Proposal, procurement: Procurement, reason: str) -> None:
    transition(proposal, "expired" if reason == "quote_expired" else "superseded", PROPOSAL_TRANSITIONS, "proposal")
    proposal.status_reason = reason
    if procurement.status == "awaiting_approval":
        set_procurement_status(procurement, "draft", f"proposal_invalid:{reason}")


def approve(
    db: Session, session: DemoSession, proposal_id: str, version: int, digest: str, now: datetime | None = None
) -> Purchase:
    """Approve the exact snapshot. Idempotent: repeated approval of the same snapshot returns the same purchase."""
    now = now or utcnow()
    proposal = db.scalar(
        select(Proposal).where(Proposal.id == proposal_id, Proposal.session_id == session.id).with_for_update()
    )
    if not proposal:
        raise not_found("proposal")
    existing = db.scalar(select(Purchase).where(Purchase.proposal_id == proposal.id))
    if existing:
        if proposal.digest != digest or proposal.version != version:
            raise AppError("approval_mismatch", "This proposal was already approved with different terms.", 409)
        return existing
    if proposal.version != version or proposal.digest != digest:
        raise AppError("stale_proposal", "The proposal changed. Review the current version before approving.", 409)
    if proposal.status != "current":
        raise AppError("proposal_not_current", f"Proposal is {proposal.status}; a fresh proposal is required.", 409)

    procurement = db.get(Procurement, proposal.procurement_id)
    quote = db.get(Quote, proposal.quote_id)
    reason = quote_staleness(db, quote, now) or ("quote_superseded" if quote.status != "active" else None)
    if reason is None:
        ev = evaluate(db, procurement, quote, now)
        if not ev["feasible"]:
            reason = ev["reasons"][0]["code"]
    if reason is None:
        supplier = db.get(Supplier, quote.supplier_id)
        if canonical_digest(build_snapshot(procurement, quote, supplier, proposal.version)) != proposal.digest:
            reason = "snapshot_mismatch"
    if reason:
        _invalidate(db, proposal, procurement, reason)
        db.commit()
        raise AppError(
            "proposal_invalidated",
            "Supplier terms changed since this proposal was made. Run the buyer again for a fresh proposal.",
            409,
            {"reason": reason},
        )

    settings = get_settings()
    lines = [{"supplier_item_id": line["supplier_item_id"], "sku": line["offered_sku"], "qty": line["qty"]} for line in quote.lines]
    for line in lines:
        # Atomic conditional increment: concurrent approvals cannot reserve more than the supplier has.
        ok = db.execute(
            text(
                "UPDATE supplier_items SET reserved = reserved + :qty "
                "WHERE id = :id AND stock - reserved >= :qty RETURNING id"
            ),
            {"qty": line["qty"], "id": line["supplier_item_id"]},
        ).first()
        if not ok:
            db.rollback()
            raise AppError("insufficient_stock", f"{line['sku']} can no longer be reserved.", 409, {"sku": line["sku"]})

    db.add(Approval(proposal_id=proposal.id, session_id=session.id, digest=digest, expires_at=now + timedelta(minutes=settings.reservation_minutes)))
    purchase = Purchase(
        session_id=session.id,
        kind="restock",
        proposal_id=proposal.id,
        status="creating",
        amount_cents=proposal.snapshot["total_cents"],
        currency="USD",
        snapshot=proposal.snapshot,
        digest=proposal.digest,
        payment_mode=settings.payment_mode,
    )
    db.add(purchase)
    db.flush()
    db.add(
        Reservation(
            purchase_id=purchase.id,
            session_id=session.id,
            supplier_id=quote.supplier_id,
            lines=lines,
            expires_at=now + timedelta(minutes=settings.reservation_minutes),
        )
    )
    transition(proposal, "approved", PROPOSAL_TRANSITIONS, "proposal")
    set_procurement_status(procurement, "approved", None)
    db.flush()
    return purchase


def release_reservation(db: Session, reservation: Reservation, reason: str) -> None:
    if reservation.status in ("released", "consumed"):
        return
    for line in reservation.lines:
        db.execute(
            text("UPDATE supplier_items SET reserved = reserved - :qty WHERE id = :id"),
            {"qty": line["qty"], "id": line["supplier_item_id"]},
        )
    transition(reservation, "released", RESERVATION_TRANSITIONS, "reservation")


def consume_reservation(db: Session, reservation: Reservation) -> None:
    """Paid: reserved units leave the supplier's stock permanently."""
    if reservation.status == "consumed":
        return
    if reservation.status == "released":
        # Capture only starts after the reservation is pinned, so this means state was corrupted elsewhere.
        raise AppError("reservation_released", "Paid purchase has no reserved stock; manual review required.", 409)
    for line in reservation.lines:
        db.execute(
            text("UPDATE supplier_items SET reserved = reserved - :qty, stock = stock - :qty WHERE id = :id"),
            {"qty": line["qty"], "id": line["supplier_item_id"]},
        )
    transition(reservation, "consumed", RESERVATION_TRANSITIONS, "reservation")


def expire_reservations(db: Session, now: datetime | None = None) -> int:
    """TTL cleanup. Never touches capture_in_progress reservations: an unknown capture pins the stock."""
    now = now or utcnow()
    count = 0
    rows = db.scalars(
        select(Reservation).where(Reservation.status == "held", Reservation.expires_at < now).with_for_update(skip_locked=True)
    ).all()
    for r in rows:
        purchase = db.get(Purchase, r.purchase_id)
        if purchase.status not in ("creating", "awaiting_paypal_approval"):
            continue
        release_reservation(db, r, "reservation_expired")
        transition(purchase, "cancelled", PURCHASE_TRANSITIONS, "purchase")
        purchase.status_reason = "reservation_expired_before_capture"
        count += 1
    return count
