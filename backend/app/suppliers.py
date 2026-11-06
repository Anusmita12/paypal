"""Simulated supplier services: authoritative quotes, deterministic negotiation, surplus, scenarios.

Supplier terms are session-scoped rows. Nothing the buyer agent sends can change them; tools only read
quotes generated here or submit bounded counteroffers that `negotiate` decides.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import AppError, not_found
from app.models import NegotiationRound, Procurement, Quote, Supplier, SupplierItem

MAX_NEGOTIATION_ROUNDS = 3
NUMERIC_MIN_ATTRS = {"power_w", "capacity_mah", "output_w", "ports"}
SUPERSET_ATTRS = {"protocols", "certifications"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def compatibility_problems(
    required_category: str, required: dict, offered_category: str, offered: dict
) -> list[str]:
    """Empty list means the offered item can stand in for the required one."""
    problems = []
    if required_category != offered_category:
        problems.append(f"category {offered_category} ≠ {required_category}")
    for key, need in required.items():
        have = offered.get(key)
        if key in NUMERIC_MIN_ATTRS:
            if not isinstance(have, int | float) or have < need:
                problems.append(f"{key} {have} < required {need}")
        elif key in SUPERSET_ATTRS:
            missing = set(need) - set(have or [])
            if missing:
                problems.append(f"{key} missing {', '.join(sorted(missing))}")
        elif have != need:
            problems.append(f"{key} {have} ≠ required {need}")
    return problems


def supplier_by_code(db: Session, session_id: str, code: str) -> Supplier:
    s = db.scalar(select(Supplier).where(Supplier.session_id == session_id, Supplier.code == code))
    if not s:
        raise not_found("supplier")
    return s


def public_supplier(s: Supplier) -> dict:
    return {
        "supplier_id": s.code,
        "name": s.name,
        "categories": s.categories,
        "lead_days": s.lead_days,
        "shipping_cents": s.shipping_cents,
        "currency": s.currency,
        "negotiable": s.max_discount_bps > 0,
        # Supplier-written text: untrusted data for display, never instructions.
        "description_untrusted": s.description,
    }


def _items(db: Session, supplier: Supplier) -> list[SupplierItem]:
    return list(db.scalars(select(SupplierItem).where(SupplierItem.supplier_id == supplier.id)))


def _business_date(procurement: Procurement) -> date:
    return date.fromisoformat(procurement.requirements["business_date"])


def build_quote(
    db: Session,
    procurement: Procurement,
    supplier: Supplier,
    *,
    substitutions: dict[str, int] | None = None,
    discount_bps: int = 0,
    parent: Quote | None = None,
    rationale_code: str = "list_price",
    now: datetime | None = None,
) -> Quote:
    """Price the full confirmed basket from current supplier terms. `substitutions` maps SKU -> surplus item id."""
    if not procurement.requirements:
        raise AppError("requirements_not_confirmed", "Confirm requirements before requesting quotes.", 409)
    now = now or utcnow()
    req = procurement.requirements
    substitutions = substitutions or {}
    items = _items(db, supplier)
    regular = {i.sku: i for i in items if not i.is_surplus}
    by_id = {i.id: i for i in items}

    lines, issues = [], []
    for line in req["lines"]:
        sku, qty = line["sku"], line["qty"]
        item = by_id.get(substitutions[sku]) if sku in substitutions else regular.get(sku)
        if item is None:
            issues.append({"sku": sku, "code": "not_carried", "message": f"{supplier.name} does not carry {sku}"})
            continue
        problems = compatibility_problems(line["category"], line["attributes"], item.category, item.attributes)
        available = item.stock - item.reserved
        if available < qty:
            issues.append({"sku": sku, "code": "insufficient_stock", "message": f"{available} available, {qty} needed"})
        if qty < item.moq:
            issues.append({"sku": sku, "code": "below_moq", "message": f"minimum order {item.moq}"})
        if problems:
            issues.append({"sku": sku, "code": "spec_mismatch", "message": "; ".join(problems)})
        lines.append(
            {
                "sku": sku,
                "offered_sku": item.offered_sku,
                "supplier_item_id": item.id,
                "name": item.name,
                "category": item.category,
                "attributes": item.attributes,
                "qty": qty,
                "unit_cents": item.unit_cents,
                "line_cents": item.unit_cents * qty,
                "is_surplus": item.is_surplus,
                "compatible": not problems,
                "compatibility_problems": problems,
            }
        )

    subtotal = sum(line["line_cents"] for line in lines)
    discount = subtotal * discount_bps // 10000
    quote = Quote(
        procurement_id=procurement.id,
        supplier_id=supplier.id,
        supplier_revision=supplier.revision,
        parent_quote_id=parent.id if parent else None,
        negotiated=parent is not None,
        lines=lines,
        issues=issues,
        subtotal_cents=subtotal,
        discount_bps=discount_bps,
        discount_cents=discount,
        shipping_cents=supplier.shipping_cents,
        tax_cents=0,  # synthetic scenario: tax excluded
        total_cents=subtotal - discount + supplier.shipping_cents,
        currency=supplier.currency,
        delivery_date=_business_date(procurement) + timedelta(days=supplier.lead_days),
        expires_at=now + timedelta(minutes=supplier.quote_ttl_minutes),
        rationale_code=rationale_code,
    )
    # A new quote replaces every earlier active quote from this supplier for this procurement.
    db.execute(
        update(Quote)
        .where(Quote.procurement_id == procurement.id, Quote.supplier_id == supplier.id, Quote.status == "active")
        .values(status="superseded")
    )
    db.add(quote)
    db.flush()
    return quote


def quote_staleness(db: Session, quote: Quote, now: datetime | None = None) -> str | None:
    now = now or utcnow()
    supplier = db.get(Supplier, quote.supplier_id)
    if supplier.revision != quote.supplier_revision:
        return "supplier_terms_changed"
    if now >= quote.expires_at:
        return "quote_expired"
    return None


def surplus_offers(db: Session, procurement: Procurement, supplier: Supplier) -> list[dict]:
    """Surplus items that relate to the basket, each with a structured compatibility verdict."""
    req_lines = {line["sku"]: line for line in procurement.requirements["lines"]}
    out = []
    for item in _items(db, supplier):
        if not item.is_surplus or item.sku not in req_lines:
            continue
        line = req_lines[item.sku]
        problems = compatibility_problems(line["category"], line["attributes"], item.category, item.attributes)
        out.append(
            {
                "surplus_sku": item.offered_sku,
                "replaces_sku": item.sku,
                "name": item.name,
                "unit_cents": item.unit_cents,
                "available": item.stock - item.reserved,
                "compatible": not problems,
                "compatibility_problems": problems,
                "note_untrusted": item.note,
            }
        )
    return out


def negotiate(
    db: Session,
    procurement: Procurement,
    base: Quote,
    requested_discount_bps: int,
    surplus_skus: list[str],
    run_id: str | None = None,
    now: datetime | None = None,
) -> tuple[NegotiationRound, Quote | None]:
    """Deterministic supplier policy. Returns the persisted round and the new quote, if any."""
    now = now or utcnow()
    supplier = db.get(Supplier, base.supplier_id)
    if base.procurement_id != procurement.id:
        raise not_found("quote")
    if base.status != "active":
        raise AppError("quote_not_current", "Negotiate against the supplier's current quote.", 409)
    stale = quote_staleness(db, base, now)
    if stale:
        raise AppError(stale, "This quote is no longer valid; request a fresh quote.", 409)
    if not 0 <= requested_discount_bps <= 5000:
        raise AppError("invalid_discount", "Requested discount must be between 0 and 5000 basis points.")

    rounds = db.scalars(
        select(NegotiationRound).where(
            NegotiationRound.procurement_id == procurement.id,
            NegotiationRound.supplier_id == supplier.id,
            NegotiationRound.run_id.is_(None) if run_id is None else NegotiationRound.run_id == run_id,
        )
    ).all()
    round_no = len(rounds) + 1
    if round_no > MAX_NEGOTIATION_ROUNDS:
        raise AppError("negotiation_limit", f"At most {MAX_NEGOTIATION_ROUNDS} negotiation rounds per supplier per run.", 409)

    # Keep surplus substitutions already agreed in the base quote, then add the newly requested ones.
    substitutions = {line["sku"]: line["supplier_item_id"] for line in base.lines if line["is_surplus"]}
    surplus = {i.offered_sku: i for i in _items(db, supplier) if i.is_surplus}
    req_lines = {line["sku"]: line for line in procurement.requirements["lines"]}
    rejected_surplus = []
    for code in surplus_skus:
        item = surplus.get(code)
        if item is None or item.sku not in req_lines:
            rejected_surplus.append({"surplus_sku": code, "reason": "unknown_or_unrelated_surplus"})
            continue
        line = req_lines[item.sku]
        problems = compatibility_problems(line["category"], line["attributes"], item.category, item.attributes)
        if problems:
            rejected_surplus.append({"surplus_sku": code, "reason": "incompatible_surplus", "problems": problems})
        else:
            substitutions[item.sku] = item.id

    def record(outcome: str, granted: int, result: Quote | None, rationale: str) -> NegotiationRound:
        r = NegotiationRound(
            procurement_id=procurement.id,
            run_id=run_id,
            supplier_id=supplier.id,
            round_no=round_no,
            base_quote_id=base.id,
            requested_discount_bps=requested_discount_bps,
            requested_surplus=list(surplus_skus),
            outcome=outcome,
            granted_discount_bps=granted,
            result_quote_id=result.id if result else None,
            rationale_code=rationale,
        )
        db.add(r)
        db.flush()
        return r

    if rejected_surplus:
        return record("rejected", 0, None, rejected_surplus[0]["reason"]), None

    items = _items(db, supplier)
    by_id = {i.id: i for i in items}
    regular = {i.sku: i for i in items if not i.is_surplus}
    subtotal = 0
    for line in procurement.requirements["lines"]:
        item = by_id.get(substitutions[line["sku"]]) if line["sku"] in substitutions else regular.get(line["sku"])
        subtotal += (item.unit_cents if item else 0) * line["qty"]
    if supplier.max_discount_bps == 0:
        cap, rationale = 0, "no_discount_policy"
    elif subtotal < supplier.min_subtotal_for_discount_cents:
        cap, rationale = 0, "below_discount_threshold"
    else:
        ladder = supplier.discount_ladder_bps or [supplier.max_discount_bps]
        cap = min(ladder[min(round_no, len(ladder)) - 1], supplier.max_discount_bps)
        rationale = "discount_floor_reached" if cap == supplier.max_discount_bps else "ladder_step"
    granted = min(requested_discount_bps, cap)
    if base.discount_bps <= cap:
        granted = max(granted, base.discount_bps)  # a concession already granted is not withdrawn
    new_subs = substitutions != {line["sku"]: line["supplier_item_id"] for line in base.lines if line["is_surplus"]}

    if granted == base.discount_bps and not new_subs:
        return record("rejected", granted, None, rationale), None
    outcome = "accepted" if granted == requested_discount_bps else "countered"
    quote = build_quote(
        db,
        procurement,
        supplier,
        substitutions=substitutions,
        discount_bps=granted,
        parent=base,
        rationale_code=f"negotiated_{rationale}",
        now=now,
    )
    return record(outcome, granted, quote, rationale), quote


# --- Scenarios: session-scoped supplier disruptions. They change supplier data only. ---

A_PRICE_INCREASE = {"CHG-65W-PD": 1800, "EAR-BT-BASIC": 1300, "CBL-C2C-1M": 305}


def apply_scenario(db: Session, session_id: str, name: str, now: datetime | None = None) -> dict:
    now = now or utcnow()
    if name == "supplier_a_price_increase":
        a = supplier_by_code(db, session_id, "supplier-a")
        changed = []
        for item in _items(db, a):
            new = A_PRICE_INCREASE.get(item.offered_sku)
            if new and item.unit_cents != new:
                changed.append({"sku": item.offered_sku, "from_cents": item.unit_cents, "to_cents": new})
                item.unit_cents = new
        if changed:
            a.revision += 1
        return {"scenario": name, "supplier_id": a.code, "revision": a.revision, "changed": changed}
    if name == "expire_quotes":
        res = db.execute(
            update(Quote)
            .where(
                Quote.status == "active",
                Quote.procurement_id.in_(select(Procurement.id).where(Procurement.session_id == session_id)),
            )
            .values(expires_at=now - timedelta(seconds=1))
        )
        return {"scenario": name, "expired_quotes": res.rowcount}
    if name == "supplier_b_stock_out":
        b = supplier_by_code(db, session_id, "supplier-b")
        item = db.scalar(
            select(SupplierItem).where(SupplierItem.supplier_id == b.id, SupplierItem.offered_sku == "CHG-65W-PD")
        )
        item.stock = item.reserved
        b.revision += 1
        return {"scenario": name, "supplier_id": b.code, "revision": b.revision}
    raise AppError("unknown_scenario", f"Unknown scenario {name!r}.", 404)
