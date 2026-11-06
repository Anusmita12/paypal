"""Prepaid tool balance and metered pay-per-call charges.

PayPal moves money at the edges (top-up via Orders, provider settlement via Payouts). Individual calls are
ledger debits: a 1–2¢ PayPal transaction per call is not viable, and each checkout needs buyer approval.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError
from app.models import LedgerEntry, PaidTool, ToolUsage
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
