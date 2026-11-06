"""Prepaid tool balance and metered pay-per-call charges.

PayPal moves money at the edges (top-up via Orders, provider settlement via Payouts). Individual calls are
ledger debits: a 1–2¢ PayPal transaction per call is not viable, and each checkout needs buyer approval.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approvals import canonical_digest
from app.config import get_settings
from app.errors import AppError
from app.models import LedgerEntry, PaidTool, Purchase, PurchaseEvent, ToolUsage
from app.suppliers import utcnow

STARTER_GRANT_CENTS = 50


def grant_starter_credit(db: Session, session_id: str) -> None:
    db.execute(text("UPDATE demo_sessions SET wallet_balance_cents = wallet_balance_cents + :c WHERE id = :id"),
               {"c": STARTER_GRANT_CENTS, "id": session_id})
    db.add(LedgerEntry(session_id=session_id, business_key=f"wallet_grant:{session_id}", kind="wallet_grant",
                       amount_cents=STARTER_GRANT_CENTS, details={"note": "Synthetic demo starter credit, not PayPal money"}))


def charge(db: Session, session_id: str, run_id: str | None, call_key: str, tool_code: str, now: datetime | None = None) -> ToolUsage:
    """Debit one paid tool call exactly once per call_key. Raises when balance or spend caps would be exceeded."""
    now = now or utcnow()
    existing = db.scalar(select(ToolUsage).where(ToolUsage.call_key == call_key))
    if existing:
        return existing
    tool = db.get(PaidTool, tool_code)
    if not tool:
        raise AppError("unknown_paid_tool", f"No paid tool {tool_code!r}.", 404)
    settings = get_settings()
    if run_id:
        run_spend = db.scalar(select(func.coalesce(func.sum(ToolUsage.price_cents), 0)).where(ToolUsage.run_id == run_id))
        if run_spend + tool.price_cents > settings.tool_spend_cap_per_run_cents:
            raise AppError("run_spend_cap", f"Run tool spend cap of {settings.tool_spend_cap_per_run_cents}¢ reached.", 402)
    day_spend = db.scalar(
        select(func.coalesce(func.sum(ToolUsage.price_cents), 0)).where(
            ToolUsage.session_id == session_id, ToolUsage.created_at >= now - timedelta(days=1)
        )
    )
    if day_spend + tool.price_cents > settings.tool_spend_cap_per_day_cents:
        raise AppError("daily_spend_cap", f"Daily tool spend cap of {settings.tool_spend_cap_per_day_cents}¢ reached.", 402)

    try:
        with db.begin_nested():
            debited = db.execute(
                text(
                    "UPDATE demo_sessions SET wallet_balance_cents = wallet_balance_cents - :p "
                    "WHERE id = :id AND wallet_balance_cents >= :p RETURNING wallet_balance_cents"
                ),
                {"p": tool.price_cents, "id": session_id},
            ).first()
            if not debited:
                raise AppError("insufficient_tool_balance", "Tool balance is too low. Top up with PayPal to continue.", 402)
            usage = ToolUsage(session_id=session_id, run_id=run_id, call_key=call_key, tool_code=tool.code,
                              provider_id=tool.provider_id, price_cents=tool.price_cents)
            db.add(usage)
            db.add(LedgerEntry(session_id=session_id, business_key=f"tool_usage:{call_key}", kind="tool_usage",
                               amount_cents=-tool.price_cents, details={"tool": tool.code, "provider": tool.provider_id}))
            db.flush()
    except IntegrityError:
        # A concurrent duplicate of the same logical call won; its charge stands, ours rolled back.
        existing = db.scalar(select(ToolUsage).where(ToolUsage.call_key == call_key))
        if existing:
            return existing
        raise
    return usage


TOPUP_AMOUNTS_CENTS = (100, 300, 500)
MAX_OPEN_TOPUPS = 3


def create_topup(db: Session, session_id: str, amount_cents: int) -> Purchase:
    """A tool-credit purchase paid through the same PayPal checkout and finalizer as restocks."""
    if amount_cents not in TOPUP_AMOUNTS_CENTS:
        raise AppError("invalid_topup_amount", "Choose one of the offered top-up amounts.", 422)
    open_count = db.scalar(select(func.count()).select_from(Purchase).where(
        Purchase.session_id == session_id, Purchase.kind == "wallet_topup",
        Purchase.status.in_(["creating", "awaiting_paypal_approval", "unknown"])))
    if open_count >= MAX_OPEN_TOPUPS:
        raise AppError("too_many_open_topups", "Finish or abandon an existing top-up first.", 429)
    snapshot = {
        "kind": "wallet_topup",
        "supplier": {"id": "tradepilot", "name": "TradePilot tool credit", "revision": 1},
        "lines": [{"sku": "TOOL-CREDIT", "offered_sku": "TOOL-CREDIT", "name": "Prepaid agent tool credit",
                   "attributes": {}, "qty": 1, "unit_cents": amount_cents, "line_cents": amount_cents, "is_surplus": False}],
        "subtotal_cents": amount_cents, "discount_bps": 0, "discount_cents": 0, "shipping_cents": 0, "tax_cents": 0,
        "total_cents": amount_cents, "currency": "USD",
        "payee_note": "Paid to the demo storefront (PayPal Sandbox business account), which settles tool providers.",
    }
    p = Purchase(session_id=session_id, kind="wallet_topup", status="creating", amount_cents=amount_cents, currency="USD",
                 snapshot=snapshot, digest=canonical_digest(snapshot), payment_mode=get_settings().payment_mode)
    db.add(p)
    db.flush()
    db.add(PurchaseEvent(purchase_id=p.id, kind="approved", source="owner",
                         summary=f"Owner started a ${amount_cents / 100:.2f} tool-credit top-up.", data={}))
    return p
