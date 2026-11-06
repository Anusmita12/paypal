"""Fixture loading. Global metadata is upserted idempotently; each demo session gets its own retailer copy."""

import csv
import json
import os
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import PaidTool, Product, Sale, Supplier, SupplierItem
from app.wallet import grant_starter_credit

FIXTURES = Path(os.environ.get("FIXTURES_DIR", Path(__file__).resolve().parents[2] / "fixtures"))


@lru_cache
def _products() -> list[dict]:
    with open(FIXTURES / "products.csv", newline="") as f:
        return list(csv.DictReader(f))


@lru_cache
def _sales() -> list[dict]:
    with open(FIXTURES / "sales.csv", newline="") as f:
        return list(csv.DictReader(f))


@lru_cache
def _suppliers() -> list[dict]:
    return json.loads((FIXTURES / "suppliers.json").read_text())["suppliers"]


@lru_cache
def _tools() -> list[dict]:
    return json.loads((FIXTURES / "tools.json").read_text())["tools"]


def seed_global(db: Session) -> int:
    """Upsert the paid-tool registry. Safe to run on every boot."""
    for t in _tools():
        stmt = insert(PaidTool).values(**t)
        db.execute(stmt.on_conflict_do_update(index_elements=["code"], set_={k: stmt.excluded[k] for k in t if k != "code"}))
    return len(_tools())


def seed_session(db: Session, session_id: str, business_date: date) -> None:
    products = {}
    for row in _products():
        attrs = json.loads(row["attributes_json"])
        history_days = int(row["history_days"])
        p = Product(
            session_id=session_id,
            sku=row["sku"],
            name=row["name"],
            category=row["category"],
            attributes=attrs,
            on_hand=int(row["on_hand"]),
            incoming=int(row["incoming"]),
            safety_stock=int(row["safety_stock"]),
            listed_since=business_date - timedelta(days=history_days),
        )
        db.add(p)
        products[p.sku] = p
    for row in _sales():
        d = business_date - timedelta(days=int(row["days_ago"]))
        db.add(Sale(session_id=session_id, sku=row["sku"], sale_date=d, units=int(row["units"]),
                    import_key=f"seed:{row['sku']}:{d.isoformat()}"))
    for s in _suppliers():
        sup = Supplier(
            session_id=session_id,
            code=s["code"],
            name=s["name"],
            description=s["description"],
            categories=s["categories"],
            lead_days=s["lead_days"],
            shipping_cents=s["shipping_cents"],
            max_discount_bps=s["max_discount_bps"],
            discount_ladder_bps=s["discount_ladder_bps"],
            min_subtotal_for_discount_cents=s["min_subtotal_for_discount_cents"],
            quote_ttl_minutes=s["quote_ttl_minutes"],
        )
        db.add(sup)
        db.flush()
        for it in s["items"]:
            base = products[it["sku"]]
            db.add(
                SupplierItem(
                    supplier_id=sup.id,
                    sku=it["sku"],
                    offered_sku=it.get("offered_sku", it["sku"]),
                    name=it.get("name", base.name),
                    category=base.category,
                    attributes=it.get("attributes", base.attributes),
                    unit_cents=it["unit_cents"],
                    stock=it["stock"],
                    moq=it.get("moq", 1),
                    is_surplus=it.get("is_surplus", False),
                    note=it.get("note", ""),
                )
            )
    grant_starter_credit(db, session_id)
    db.flush()
