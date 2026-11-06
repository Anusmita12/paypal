"""Transparent restocking heuristic (not forecasting).

daily_sales   = units_sold_in_last_28_complete_days / 28
target_units  = ceil(daily_sales * coverage_days) + safety_stock_units
shortage      = max(0, target_units - on_hand - confirmed_incoming)
"""

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from fractions import Fraction
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Product, Sale

WINDOW_DAYS = 28
FORMULA = (
    "daily_sales = units_sold_last_28_days / 28; "
    "target = ceil(daily_sales × coverage_days) + safety_stock; "
    "need = max(0, target − on_hand − incoming)"
)


@dataclass
class RestockLine:
    sku: str
    name: str
    category: str
    on_hand: int
    incoming: int
    safety_stock: int
    units_28d: int
    days_with_history: int
    daily_sales: str  # exact fraction rendered as decimal string, for display only
    target_units: int
    need: int
    status: str  # shortage | ok
    warning: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def window(business_date: date) -> tuple[date, date]:
    """The last 28 complete days before the business date (inclusive bounds)."""
    return business_date - timedelta(days=WINDOW_DAYS), business_date - timedelta(days=1)


def compute_need(units_28d: int, coverage_days: int, safety_stock: int, on_hand: int, incoming: int) -> tuple[int, int]:
    daily = Fraction(units_28d, WINDOW_DAYS)
    target = ceil(daily * coverage_days) + safety_stock
    return target, max(0, target - on_hand - incoming)


def restock_lines(db: Session, session_id: str, business_date: date, coverage_days: int) -> list[RestockLine]:
    start, end = window(business_date)
    sales = {
        sku: units
        for sku, units in db.execute(
            select(Sale.sku, func.sum(Sale.units))
            .where(Sale.session_id == session_id, Sale.sale_date.between(start, end))
            .group_by(Sale.sku)
        )
    }
    out = []
    for p in db.scalars(select(Product).where(Product.session_id == session_id).order_by(Product.sku)):
        units = int(sales.get(p.sku) or 0)
        history = max(0, min(WINDOW_DAYS, (end - max(p.listed_since, start)).days + 1))
        target, need = compute_need(units, coverage_days, p.safety_stock, p.on_hand, p.incoming)
        warning = None
        if units == 0:
            warning = "No sales in the last 28 days; need is safety stock only. Enter a quantity if you expect demand."
        elif history < WINDOW_DAYS:
            warning = f"Sales history covers only {history} of {WINDOW_DAYS} days; the 28-day average may understate demand."
        out.append(
            RestockLine(
                sku=p.sku,
                name=p.name,
                category=p.category,
                on_hand=p.on_hand,
                incoming=p.incoming,
                safety_stock=p.safety_stock,
                units_28d=units,
                days_with_history=history,
                daily_sales=f"{float(Fraction(units, WINDOW_DAYS)):.2f}",
                target_units=target,
                need=need,
                status="shortage" if need > 0 else "ok",
                warning=warning,
            )
        )
    return out
