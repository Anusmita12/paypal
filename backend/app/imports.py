"""Bounded CSV imports: validate into a stored preview, then apply atomically after the owner confirms.

Inventory imports set counted on-hand quantities. Sales imports append rows keyed by a stable sale ID, so
re-importing the same file cannot double count. Cells are data only; nothing in them is interpreted.
"""

import csv
import io
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, not_found
from app.models import DemoSession, ImportBatch, Product, Sale

MAX_BYTES = 256 * 1024
MAX_ROWS = 2000
MAX_ERRORS_SHOWN = 50
SCHEMAS = {
    "inventory": ["sku", "on_hand"],
    "sales": ["sale_id", "sku", "date", "units"],
}


def _int(value: str, lo: int, hi: int) -> int | None:
    try:
        n = int(value.strip())
    except ValueError:
        return None
    return n if lo <= n <= hi else None


def preview(db: Session, session: DemoSession, kind: str, text: str) -> ImportBatch:
    if kind not in SCHEMAS:
        raise AppError("unknown_import_kind", f"Import kind must be one of {', '.join(SCHEMAS)}.", 422)
    if len(text.encode()) > MAX_BYTES:
        raise AppError("import_too_large", f"CSV is larger than {MAX_BYTES // 1024} KB.", 413)
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [h.strip() for h in (reader.fieldnames or [])]
    if header != SCHEMAS[kind]:
        raise AppError("bad_import_header", f"Expected columns exactly: {','.join(SCHEMAS[kind])}.", 422,
                       {"got": header[:10]})
    skus = set(db.scalars(select(Product.sku).where(Product.session_id == session.id)))
    existing_keys = set(db.scalars(select(Sale.import_key).where(Sale.session_id == session.id))) if kind == "sales" else set()
    rows, errors, seen = [], [], set()
    for n, raw in enumerate(reader, start=2):  # row 1 is the header
        if len(rows) + len(errors) >= MAX_ROWS:
            raise AppError("import_too_many_rows", f"CSV has more than {MAX_ROWS} data rows.", 413)
        if None in raw or any(v is None for v in raw.values()):
            errors.append({"row": n, "message": "Wrong number of columns."})
            continue
        r = {k.strip(): (v or "").strip() for k, v in raw.items()}
        problems = []
        if r["sku"] not in skus:
            problems.append(f"unknown SKU {r['sku'][:40]!r}")
        if kind == "inventory":
            qty = _int(r["on_hand"], 0, 100_000)
            if qty is None:
                problems.append("on_hand must be a whole number 0–100000")
            key = r["sku"]
            if key in seen:
                problems.append("SKU appears twice in this file")
            row = {"sku": r["sku"], "on_hand": qty}
        else:
            units = _int(r["units"], 0, 10_000)
            if units is None:
                problems.append("units must be a whole number 0–10000")
            try:
                day = date.fromisoformat(r["date"])
                if not session.business_date - timedelta(days=365) <= day < session.business_date:
                    problems.append("date must be within the past year and before the business date")
            except ValueError:
                day = None
                problems.append("date must be YYYY-MM-DD")
            key = r["sale_id"]
            if not key or len(key) > 100:
                problems.append("sale_id is required (max 100 characters)")
            elif key in seen:
                problems.append("sale_id appears twice in this file")
            elif f"import:{key}" in existing_keys:
                problems.append("sale_id was already imported")
            row = {"sale_id": key, "sku": r["sku"], "date": day.isoformat() if day else None, "units": units}
        seen.add(key)
        if problems:
            errors.append({"row": n, "message": "; ".join(problems)})
        else:
            rows.append(row)
    if not rows and not errors:
        raise AppError("import_empty", "CSV has no data rows.", 422)
    batch = ImportBatch(session_id=session.id, kind=kind, rows=rows, errors=errors)
    db.add(batch)
    db.flush()
    return batch


def apply(db: Session, session: DemoSession, batch_id: str) -> ImportBatch:
    batch = db.scalar(select(ImportBatch).where(ImportBatch.id == batch_id, ImportBatch.session_id == session.id)
                      .with_for_update())
    if not batch:
        raise not_found("import")
    if batch.status == "applied":
        return batch
    if batch.errors:
        raise AppError("import_has_errors", "Fix the rows with errors and preview again; nothing was applied.", 409)
    if batch.kind == "inventory":
        products = {p.sku: p for p in db.scalars(select(Product).where(Product.session_id == session.id))}
        for row in batch.rows:
            products[row["sku"]].on_hand = row["on_hand"]
    else:
        existing = set(db.scalars(select(Sale.import_key).where(Sale.session_id == session.id)))
        if any(f"import:{row['sale_id']}" in existing for row in batch.rows):
            raise AppError("import_stale", "Some sale IDs were imported since this preview. Preview again.", 409)
        for row in batch.rows:
            db.add(Sale(session_id=session.id, sku=row["sku"], sale_date=date.fromisoformat(row["date"]),
                        units=row["units"], import_key=f"import:{row['sale_id']}"))
    batch.status = "applied"
    db.flush()
    return batch


def view(batch: ImportBatch) -> dict:
    return {
        "import_id": batch.id,
        "kind": batch.kind,
        "status": batch.status,
        "valid_rows": len(batch.rows),
        "error_count": len(batch.errors),
        "errors": batch.errors[:MAX_ERRORS_SHOWN],
        "rows_preview": batch.rows[:20],
    }
