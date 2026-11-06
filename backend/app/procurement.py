"""Procurement requirements: suggestions from text are proposals; only confirmed requirements bind."""

import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import AppError, not_found
from app.models import (
    PROCUREMENT_TRANSITIONS,
    PROPOSAL_TRANSITIONS,
    DemoSession,
    Procurement,
    Product,
    Proposal,
    Quote,
    transition,
)
from app.restock import restock_lines

MAX_LINES = 20
MAX_QTY = 500
MIN_BUDGET_CENTS, MAX_BUDGET_CENTS = 1_000, 1_000_000
MAX_DEADLINE_DAYS = 60
DEFAULT_COVERAGE_DAYS = 14

_NON_USD = re.compile(r"(€|£|₹|¥|\b(eur|euros?|gbp|pounds?|inr|rupees?|rs\.?|jpy|yen|cad|aud)\b)", re.IGNORECASE)
_USD_AMOUNT = re.compile(r"(?:\$\s?(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)|(\d{1,6}(?:\.\d{1,2})?)\s?(?:usd|dollars?)\b)", re.IGNORECASE)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def set_procurement_status(p: Procurement, target: str, reason: str | None = None) -> None:
    transition(p, target, PROCUREMENT_TRANSITIONS, "procurement")
    p.status_reason = reason


def suggest_from_text(text: str, business_date: date) -> dict:
    """Deterministic hints shown in the confirmation form. Never binding until the owner confirms them."""
    hints: dict = {"budget_cents": None, "deadline": None, "notes": []}
    if _NON_USD.search(text):
        hints["notes"].append("Only USD is supported. Enter the budget in US dollars; no currency conversion is done.")
    amounts = _USD_AMOUNT.findall(text)
    if len(amounts) == 1:
        raw = (amounts[0][0] or amounts[0][1]).replace(",", "")
        hints["budget_cents"] = round(float(raw) * 100)
        hints["notes"].append("Budget suggested from your text. Confirm it before suppliers are contacted.")
    elif len(amounts) > 1:
        hints["notes"].append("Several amounts found; enter the budget explicitly.")
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", text, re.IGNORECASE):
            days_ahead = (i - business_date.weekday()) % 7 or 7
            hints["deadline"] = (business_date + timedelta(days=days_ahead)).isoformat()
            hints["notes"].append(f"Deadline suggested as the next {name.title()}. Confirm the exact date.")
            break
    return hints


def create_procurement(db: Session, session: DemoSession, request_text: str, language: str) -> Procurement:
    if len(request_text) > 2000:
        raise AppError("request_too_long", "Keep the request under 2000 characters.")
    lines = [
        {"sku": line.sku, "qty": line.need}
        for line in restock_lines(db, session.id, session.business_date, DEFAULT_COVERAGE_DAYS)
        if line.need > 0
    ]
    hints = suggest_from_text(request_text, session.business_date)
    p = Procurement(
        session_id=session.id,
        request_text=request_text,
        language=language,
        status="needs_input",
        proposed={
            "lines": lines,
            "coverage_days": DEFAULT_COVERAGE_DAYS,
            "budget_cents": hints["budget_cents"],
            "deadline": hints["deadline"],
            "currency": "USD",
            "notes": hints["notes"],
            "source": "restock_formula_and_text_hints",
        },
    )
    db.add(p)
    db.flush()
    return p


def get_procurement(db: Session, session_id: str, procurement_id: str, *, lock: bool = False) -> Procurement:
    stmt = select(Procurement).where(Procurement.id == procurement_id, Procurement.session_id == session_id)
    if lock:
        stmt = stmt.with_for_update()
    p = db.scalar(stmt)
    if not p:
        raise not_found("procurement")
    return p


def confirm_requirements(
    db: Session,
    session: DemoSession,
    p: Procurement,
    *,
    lines: list[dict],
    budget_cents: int,
    deadline: date,
    coverage_days: int,
    currency: str,
) -> Procurement:
    if p.status in ("approved", "planning", "negotiating"):
        raise AppError("procurement_locked", f"Requirements cannot change while the procurement is {p.status}.", 409)
    if currency != "USD":
        raise AppError("unsupported_currency", "Only USD is supported; amounts are not converted.")
    if not 1 <= len(lines) <= MAX_LINES:
        raise AppError("invalid_lines", f"Provide between 1 and {MAX_LINES} lines.")
    if not MIN_BUDGET_CENTS <= budget_cents <= MAX_BUDGET_CENTS:
        raise AppError("invalid_budget", "Budget must be between $10.00 and $10,000.00.")
    if not 1 <= coverage_days <= 60:
        raise AppError("invalid_coverage", "Coverage days must be between 1 and 60.")
    earliest, latest = session.business_date + timedelta(days=1), session.business_date + timedelta(days=MAX_DEADLINE_DAYS)
    if not earliest <= deadline <= latest:
        raise AppError("invalid_deadline", f"Deadline must be between {earliest} and {latest}.")
    products = {pr.sku: pr for pr in db.scalars(select(Product).where(Product.session_id == session.id))}
    seen, req_lines = set(), []
    for line in lines:
        sku, qty = line["sku"], line["qty"]
        if sku in seen:
            raise AppError("duplicate_sku", f"{sku} appears more than once.")
        seen.add(sku)
        if sku not in products:
            raise AppError("unknown_sku", f"Unknown SKU {sku!r}.")
        if not 1 <= qty <= MAX_QTY:
            raise AppError("invalid_quantity", f"{sku}: quantity must be between 1 and {MAX_QTY}.")
        pr = products[sku]
        req_lines.append({"sku": sku, "name": pr.name, "category": pr.category, "attributes": pr.attributes, "qty": qty})

    # Changing requirements retires earlier quotes and proposals for this procurement.
    db.execute(update(Quote).where(Quote.procurement_id == p.id, Quote.status == "active").values(status="superseded"))
    for prop in db.scalars(select(Proposal).where(Proposal.procurement_id == p.id, Proposal.status == "current")):
        transition(prop, "superseded", PROPOSAL_TRANSITIONS, "proposal")
        prop.status_reason = "requirements_changed"

    p.requirements = {
        "lines": req_lines,
        "budget_cents": budget_cents,
        "currency": "USD",
        "deadline": deadline.isoformat(),
        "coverage_days": coverage_days,
        "business_date": session.business_date.isoformat(),
        "timezone": session.timezone,
    }
    p.confirmed_at = datetime.now(UTC)
    set_procurement_status(p, "draft", "requirements_confirmed")
    return p
