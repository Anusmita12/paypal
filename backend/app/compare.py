"""Deterministic offer feasibility and ranking. The model may explain this result but cannot change it."""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Procurement, Quote, Supplier
from app.suppliers import quote_staleness, utcnow


def evaluate(db: Session, procurement: Procurement, quote: Quote, now: datetime | None = None) -> dict:
    req = procurement.requirements
    supplier = db.get(Supplier, quote.supplier_id)
    reasons: list[dict] = []
    stale = quote_staleness(db, quote, now)
    if stale:
        reasons.append({"code": stale, "message": "Quote is no longer valid; a fresh quote is required."})
    required = {line["sku"]: line["qty"] for line in req["lines"]}
    quoted = {line["sku"]: line["qty"] for line in quote.lines}
    missing = [sku for sku in required if quoted.get(sku) != required[sku]]
    if missing:
        reasons.append({"code": "incomplete_basket", "message": f"Does not cover: {', '.join(missing)}"})
    for issue in quote.issues:
        reasons.append({"code": issue["code"], "message": f"{issue['sku']}: {issue['message']}"})
    if quote.currency != "USD":
        reasons.append({"code": "unsupported_currency", "message": f"Quoted in {quote.currency}; only USD is supported."})
    deadline = date.fromisoformat(req["deadline"])
    if quote.delivery_date > deadline:
        reasons.append(
            {"code": "late_delivery", "message": f"Delivers {quote.delivery_date.isoformat()}, after {deadline.isoformat()}"}
        )
    over = quote.total_cents - req["budget_cents"]
    if over > 0:
        reasons.append({"code": "over_budget", "message": f"${quote.total_cents / 100:.2f} exceeds budget by ${over / 100:.2f}"})
    return {
        "quote_id": quote.id,
        "supplier_id": supplier.code,
        "supplier_name": supplier.name,
        "negotiated": quote.negotiated,
        "supplier_revision": quote.supplier_revision,
        "subtotal_cents": quote.subtotal_cents,
        "discount_cents": quote.discount_cents,
        "discount_bps": quote.discount_bps,
        "shipping_cents": quote.shipping_cents,
        "tax_cents": quote.tax_cents,
        "total_cents": quote.total_cents,
        "currency": quote.currency,
        "delivery_date": quote.delivery_date.isoformat(),
        "expires_at": quote.expires_at.isoformat(),
        "spec_compliant": all(line["compatible"] for line in quote.lines),
        "uses_surplus": any(line["is_surplus"] for line in quote.lines),
        "feasible": not reasons,
        "reasons": reasons,
    }


def _latest(db: Session, procurement: Procurement, *, negotiated: bool | None = None, active_only: bool) -> dict[int, Quote]:
    stmt = select(Quote).where(Quote.procurement_id == procurement.id).order_by(Quote.created_at, Quote.id)
    if active_only:
        stmt = stmt.where(Quote.status == "active")
    if negotiated is not None:
        stmt = stmt.where(Quote.negotiated.is_(negotiated))
    latest: dict[int, Quote] = {}
    for q in db.scalars(stmt):
        latest[q.supplier_id] = q  # later rows overwrite earlier ones
    return latest


def compare(db: Session, procurement: Procurement, now: datetime | None = None) -> dict:
    now = now or utcnow()
    offers = [evaluate(db, procurement, q, now) for q in _latest(db, procurement, active_only=True).values()]
    feasible = sorted(
        (o for o in offers if o["feasible"]),
        key=lambda o: (o["total_cents"], o["delivery_date"], o["supplier_id"]),
    )
    for rank, o in enumerate(feasible, start=1):
        o["rank"] = rank
    best = feasible[0] if feasible else None

    # Savings baseline: cheapest current, non-negotiated quote that meets specs, stock and deadline
    # for the same quantities (budget ignored), with the same shipping and tax treatment.
    baseline = None
    for q in _latest(db, procurement, negotiated=False, active_only=False).values():
        ev = evaluate(db, procurement, q, now)
        eligible = all(r["code"] == "over_budget" for r in ev["reasons"])
        if eligible and (baseline is None or ev["total_cents"] < baseline["total_cents"]):
            baseline = ev
    savings = None
    if best and baseline:
        savings = max(0, baseline["total_cents"] - best["total_cents"])

    return {
        "offers": sorted(offers, key=lambda o: (not o["feasible"], o["total_cents"], o["supplier_id"])),
        "best_quote_id": best["quote_id"] if best else None,
        "baseline": (
            {"quote_id": baseline["quote_id"], "supplier_id": baseline["supplier_id"], "total_cents": baseline["total_cents"]}
            if baseline
            else None
        ),
        "savings_cents": savings,
        "suggestions": [] if best else suggestions(procurement, offers),
        "budget_cents": procurement.requirements["budget_cents"],
        "deadline": procurement.requirements["deadline"],
        "evaluated_at": now.isoformat(),
    }


def suggestions(procurement: Procurement, offers: list[dict]) -> list[str]:
    """Concrete changes the owner could approve when nothing is feasible. Never applied automatically."""
    out = []
    for o in offers:
        codes = {r["code"] for r in o["reasons"]}
        if codes == {"over_budget"}:
            out.append(f"Raise the budget to ${o['total_cents'] / 100:.2f} to accept {o['supplier_name']}.")
        elif codes == {"late_delivery"}:
            out.append(f"Move the deadline to {o['delivery_date']} to accept {o['supplier_name']}.")
        elif codes & {"supplier_terms_changed", "quote_expired"}:
            out.append(f"Request a fresh quote from {o['supplier_name']}.")
    if not out:
        out.append("Reduce quantities or remove SKUs; no supplier can meet every constraint as stated.")
    return out
