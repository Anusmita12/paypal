import csv
import io
import json

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import approvals, disputes, imports, payments, payouts, webhooks
from app.agent import start_run
from app.compare import compare
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError, not_found
from app.models import (
    AgentEvent,
    AgentRun,
    Capture,
    DemoSession,
    Dispute,
    LedgerEntry,
    NegotiationRound,
    PaidTool,
    Procurement,
    Proposal,
    ProviderOperation,
    Purchase,
    PurchaseEvent,
    Quote,
    Reservation,
    Shipment,
    Supplier,
    ToolUsage,
)
from app.procurement import confirm_requirements, create_procurement, get_procurement
from app.restock import FORMULA, WINDOW_DAYS, restock_lines, window
from app.schemas import (
    ApproveIn,
    ConfirmIn,
    CreateProcurementIn,
    DisputeApproveIn,
    ImportIn,
    SimulateDisputeIn,
    TopupIn,
)
from app.security import create_session, current_session
from app.suppliers import apply_scenario, utcnow
from app.wallet import TOPUP_AMOUNTS_CENTS, create_topup

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.cli import ALEMBIC_INI

    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    current = db.scalar(text("SELECT version_num FROM alembic_version"))
    if current != head:
        raise AppError("migrations_pending", f"Database at {current}, code expects {head}.", 503)
    return {"status": "ready", "migration": current}


@router.get("/config")
def public_config(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "ai_mode": settings.ai_mode,
        "payment_mode": settings.payment_mode,
        "channel3_mode": settings.channel3_mode,
        "scenarios_enabled": settings.enable_scenarios,
        "paypal_client_id": settings.paypal_client_id if settings.payment_mode == "paypal_sandbox" else None,
        "ai_model": settings.ai_model,
        "currency": "USD",
        "limits": {
            "max_tool_calls": settings.ai_max_tool_calls,
            "max_run_seconds": settings.ai_max_run_seconds,
            "reservation_minutes": settings.reservation_minutes,
            "tool_spend_cap_per_run_cents": settings.tool_spend_cap_per_run_cents,
        },
    }


# --- demo session ---


def _session_view(s: DemoSession) -> dict:
    return {
        "session_id": s.id,
        "business_date": s.business_date.isoformat(),
        "timezone": s.timezone,
        "expires_at": s.expires_at.isoformat(),
        "wallet_balance_cents": s.wallet_balance_cents,
    }


@router.post("/demo/session")
def new_session(request: Request, response: Response, db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings)) -> dict:
    s, csrf = create_session(db, request, response, settings)
    return {**_session_view(s), "csrf_token": csrf}


@router.get("/demo/session")
def get_session(s: DemoSession = Depends(current_session)) -> dict:
    return _session_view(s)


@router.post("/demo/scenarios/{name}")
def scenario(name: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)) -> dict:
    if not settings.enable_scenarios:
        raise AppError("scenarios_disabled", "Scenarios are disabled.", 403)
    result = apply_scenario(db, s.id, name)
    db.commit()
    return result


# --- inventory ---


@router.get("/inventory")
def inventory(coverage_days: int = Query(14, ge=1, le=60), s: DemoSession = Depends(current_session),
              db: Session = Depends(get_db)) -> dict:
    lines = restock_lines(db, s.id, s.business_date, coverage_days)
    start, end = window(s.business_date)
    return {
        "business_date": s.business_date.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": WINDOW_DAYS},
        "coverage_days": coverage_days,
        "formula": FORMULA,
        "products": [line.as_dict() for line in lines],
        "shortage_count": sum(1 for line in lines if line.need > 0),
    }


@router.get("/sales/summary")
def sales_summary(s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    lines = restock_lines(db, s.id, s.business_date, 14)
    start, end = window(s.business_date)
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "by_sku": [{"sku": line.sku, "units_28d": line.units_28d, "daily_sales": line.daily_sales,
                    "days_with_history": line.days_with_history} for line in lines],
        "total_units": sum(line.units_28d for line in lines),
    }


@router.post("/imports/preview")
def import_preview(body: ImportIn, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    batch = imports.preview(db, s, body.kind, body.csv_text)
    db.commit()
    return imports.view(batch)


@router.post("/imports/{import_id}/apply")
def import_apply(import_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    batch = imports.apply(db, s, import_id)
    db.commit()
    return imports.view(batch)


# --- procurement ---


def _procurement_view(db: Session, p: Procurement) -> dict:
    run = db.scalar(select(AgentRun).where(AgentRun.procurement_id == p.id).order_by(AgentRun.created_at.desc()))
    events = []
    if run:
        events = [
            {"seq": e.seq, "kind": e.kind, "tool": e.tool_name, "summary": e.summary, "ok": e.ok,
             "arguments": e.arguments, "elapsed_ms": e.elapsed_ms, "at": e.created_at.isoformat()}
            for e in db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.seq))
        ]
    proposal = db.scalar(select(Proposal).where(Proposal.procurement_id == p.id).order_by(Proposal.version.desc()))
    suppliers = {s.id: s for s in db.scalars(select(Supplier).where(Supplier.session_id == p.session_id))}
    rounds = [
        {"supplier_id": suppliers[r.supplier_id].code, "round": r.round_no, "requested_bps": r.requested_discount_bps,
         "requested_surplus": r.requested_surplus, "outcome": r.outcome, "granted_bps": r.granted_discount_bps,
         "rationale_code": r.rationale_code, "result_quote_id": r.result_quote_id}
        for r in db.scalars(select(NegotiationRound).where(NegotiationRound.procurement_id == p.id)
                            .order_by(NegotiationRound.created_at))
    ]
    quotes = [
        {"quote_id": q.id, "supplier_id": suppliers[q.supplier_id].code, "status": q.status, "negotiated": q.negotiated,
         "total_cents": q.total_cents, "lines": q.lines, "issues": q.issues, "created_at": q.created_at.isoformat()}
        for q in db.scalars(select(Quote).where(Quote.procurement_id == p.id).order_by(Quote.created_at))
    ]
    has_quotes = bool(quotes)
    purchase = db.scalar(select(Purchase).where(Purchase.proposal_id == proposal.id)) if proposal else None
    return {
        "procurement_id": p.id,
        "status": p.status,
        "status_reason": p.status_reason,
        "request_text": p.request_text,
        "language": p.language,
        "proposed": p.proposed,
        "requirements": p.requirements,
        "confirmed_at": p.confirmed_at.isoformat() if p.confirmed_at else None,
        "run": run and {
            "run_id": run.id, "status": run.status, "mode": run.mode, "model": run.model, "tool_calls": run.tool_calls,
            "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
            "tool_spend_cents": run.tool_spend_cents, "error_code": run.error_code, "error_message": run.error_message,
            "summary": run.summary,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        },
        "events": events,
        "negotiation_rounds": rounds,
        "quotes": quotes,
        "comparison": compare(db, p) if p.requirements and has_quotes else None,
        "proposal": proposal and {
            "proposal_id": proposal.id, "version": proposal.version, "status": proposal.status,
            "status_reason": proposal.status_reason, "snapshot": proposal.snapshot, "digest": proposal.digest,
            "explanation": proposal.explanation, "comparison": proposal.comparison,
        },
        "purchase_id": purchase.id if purchase else None,
    }


@router.post("/procurements")
def new_procurement(body: CreateProcurementIn, s: DemoSession = Depends(current_session),
                    db: Session = Depends(get_db)) -> dict:
    p = create_procurement(db, s, body.request_text, body.language)
    db.commit()
    return _procurement_view(db, p)


@router.get("/procurements")
def list_procurements(s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Procurement).where(Procurement.session_id == s.id).order_by(Procurement.created_at.desc()))
    return {"procurements": [{"procurement_id": p.id, "status": p.status, "request_text": p.request_text,
                              "created_at": p.created_at.isoformat()} for p in rows]}


@router.get("/procurements/{procurement_id}")
def procurement_detail(procurement_id: str, s: DemoSession = Depends(current_session),
                       db: Session = Depends(get_db)) -> dict:
    return _procurement_view(db, get_procurement(db, s.id, procurement_id))


@router.post("/procurements/{procurement_id}/confirm")
def confirm(procurement_id: str, body: ConfirmIn, s: DemoSession = Depends(current_session),
            db: Session = Depends(get_db)) -> dict:
    p = get_procurement(db, s.id, procurement_id, lock=True)
    confirm_requirements(db, s, p, lines=[line.model_dump() for line in body.lines], budget_cents=body.budget_cents,
                         deadline=body.deadline, coverage_days=body.coverage_days, currency=body.currency)
    db.commit()
    return _procurement_view(db, p)


@router.post("/procurements/{procurement_id}/run", status_code=202)
def run(procurement_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    p = get_procurement(db, s.id, procurement_id, lock=True)
    r = start_run(db, s, p)
    db.commit()
    return {"run_id": r.id, "status": r.status, "mode": r.mode}


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: str, body: ApproveIn, s: DemoSession = Depends(current_session),
            db: Session = Depends(get_db)) -> dict:
    purchase = approvals.approve(db, s, proposal_id, body.version, body.digest)
    db.commit()
    return _purchase_view(db, purchase)


# --- webhooks (no session; provider signature verification instead of CSRF) ---


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise AppError("payload_too_large", "Webhook body too large.", 413)
    try:
        event = json.loads(raw)
    except ValueError as exc:
        raise AppError("invalid_webhook", "Body is not JSON.", 400) from exc
    headers = {k.lower(): v for k, v in request.headers.items()}
    return webhooks.receive(db, headers, event)


# --- purchases and wallet ---


def _purchase_view(db: Session, p: Purchase, detail: bool = False) -> dict:
    res = db.scalar(select(Reservation).where(Reservation.purchase_id == p.id))
    cap = db.scalar(select(Capture).where(Capture.purchase_id == p.id))
    view = {
        "purchase_id": p.id,
        "kind": p.kind,
        "status": p.status,
        "status_reason": p.status_reason,
        "amount_cents": p.amount_cents,
        "currency": p.currency,
        "snapshot": p.snapshot,
        "digest": p.digest,
        "paypal_order_id": p.paypal_order_id,
        "capture_id": cap.id if cap else None,
        "payment_mode": p.payment_mode,
        "reservation": res and {"status": res.status, "expires_at": res.expires_at.isoformat(), "lines": res.lines},
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }
    if detail:
        view["events"] = [
            {"kind": e.kind, "source": e.source, "summary": e.summary, "at": e.created_at.isoformat()}
            for e in db.scalars(select(PurchaseEvent).where(PurchaseEvent.purchase_id == p.id)
                                .order_by(PurchaseEvent.created_at, PurchaseEvent.id))
        ]
        view["operations"] = [
            {"kind": o.kind, "request_id": o.request_id, "state": o.state, "attempts": o.attempts,
             "payload_digest": o.payload_digest, "last_error": o.last_error}
            for o in db.scalars(select(ProviderOperation).where(ProviderOperation.purchase_id == p.id)
                                .order_by(ProviderOperation.id))
        ]
        view["capture"] = cap and {"id": cap.id, "status": cap.status, "amount_cents": cap.amount_cents,
                                   "currency": cap.currency, "source": cap.source, "at": cap.created_at.isoformat()}
        sh = db.scalar(select(Shipment).where(Shipment.purchase_id == p.id))
        view["shipment"] = _shipment_view(sh) if sh else None
        view["disputes"] = [{"dispute_id": d.id, "paypal_dispute_id": d.paypal_dispute_id, "status": d.status}
                            for d in db.scalars(select(Dispute).where(Dispute.purchase_id == p.id))]
        view["ledger"] = [
            {"kind": e.kind, "business_key": e.business_key, "amount_cents": e.amount_cents, "at": e.created_at.isoformat()}
            for e in db.scalars(select(LedgerEntry).where(LedgerEntry.purchase_id == p.id))
        ]
    return view


def _owned_purchase(db: Session, s: DemoSession, purchase_id: str) -> Purchase:
    p = db.scalar(select(Purchase).where(Purchase.id == purchase_id, Purchase.session_id == s.id))
    if not p:
        raise not_found("purchase")
    return p


@router.get("/purchases")
def purchases(s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Purchase).where(Purchase.session_id == s.id).order_by(Purchase.created_at.desc()))
    return {"purchases": [_purchase_view(db, p) for p in rows]}


@router.get("/purchases/{purchase_id}")
def purchase_detail(purchase_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    return _purchase_view(db, _owned_purchase(db, s, purchase_id), detail=True)


@router.post("/purchases/{purchase_id}/paypal-order")
def paypal_order(purchase_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    p = payments.ensure_order(db, s.id, purchase_id)
    return _purchase_view(db, p, detail=True)


@router.post("/purchases/{purchase_id}/fixture-approve")
def fixture_approve(purchase_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    p = payments.fixture_buyer_approve(db, s.id, purchase_id)
    return _purchase_view(db, p, detail=True)


@router.post("/purchases/{purchase_id}/capture")
def capture(purchase_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    p = payments.capture(db, s.id, purchase_id)
    return _purchase_view(db, p, detail=True)


@router.post("/purchases/{purchase_id}/reconcile")
def reconcile(purchase_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    _owned_purchase(db, s, purchase_id)
    recent = db.scalar(
        select(PurchaseEvent.created_at).where(PurchaseEvent.purchase_id == purchase_id, PurchaseEvent.kind == "reconcile")
        .order_by(PurchaseEvent.created_at.desc()).limit(1)
    )
    if recent and (utcnow() - recent).total_seconds() < 3:
        raise AppError("rate_limited", "Status was just checked; wait a few seconds.", 429)
    try:
        p = payments.reconcile(db, purchase_id, source="owner_check")
    except payments.ProviderTimeout as exc:
        raise AppError("provider_unavailable", "PayPal is not responding; try again shortly.", 503) from exc
    return _purchase_view(db, p, detail=True)


def _csv_safe(value: object) -> str:
    """Neutralize spreadsheet formula prefixes in exported cells."""
    text_value = "" if value is None else str(value)
    return "'" + text_value if text_value[:1] in ("=", "+", "-", "@", "\t", "\r") else text_value


@router.get("/purchases/{purchase_id}/evidence")
def evidence(purchase_id: str, format: str = Query("json", pattern="^(json|csv)$"),
             s: DemoSession = Depends(current_session), db: Session = Depends(get_db)):
    view = _purchase_view(db, _owned_purchase(db, s, purchase_id), detail=True)
    if format == "json":
        return Response(
            content=json.dumps({"generated_at": utcnow().isoformat(), "purchase": view}, indent=2),
            media_type="application/json",
            headers={"content-disposition": f'attachment; filename="purchase-{purchase_id}.json"'},
        )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["purchase_id", "status", "paypal_order_id", "capture_id", "supplier", "sku", "offered_sku", "name",
                "qty", "unit_cents", "line_cents", "total_cents", "currency"])
    snap = view["snapshot"]
    for line in snap["lines"]:
        w.writerow([_csv_safe(v) for v in (view["purchase_id"], view["status"], view["paypal_order_id"], view["capture_id"],
                                           snap["supplier"]["name"], line["sku"], line["offered_sku"], line["name"],
                                           line["qty"], line["unit_cents"], line["line_cents"], view["amount_cents"],
                                           view["currency"])])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"content-disposition": f'attachment; filename="purchase-{purchase_id}.csv"'})


@router.get("/wallet")
def wallet(s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    tools = {t.code: t for t in db.scalars(select(PaidTool))}
    batch_rows = payouts.batches_for(db, s.id)
    batch_status = {b["payout_id"]: b["status"] for b in batch_rows}
    usage = db.scalars(select(ToolUsage).where(ToolUsage.session_id == s.id).order_by(ToolUsage.created_at.desc()).limit(500))
    ledger = db.scalars(select(LedgerEntry).where(LedgerEntry.session_id == s.id).order_by(LedgerEntry.created_at.desc()).limit(200))
    return {
        "balance_cents": s.wallet_balance_cents,
        "unsettled_cents": payouts.unsettled_cents(db, s.id),
        "topup_amounts_cents": list(TOPUP_AMOUNTS_CENTS),
        "tools": [{"code": t.code, "name": t.name, "provider_id": t.provider_id, "provider_name": t.provider_name,
                   "price_cents": t.price_cents} for t in tools.values()],
        "usage": [{"id": u.id, "run_id": u.run_id, "tool_code": u.tool_code, "tool_name": tools[u.tool_code].name,
                   "provider_id": u.provider_id, "price_cents": u.price_cents,
                   "payout_status": batch_status.get(u.payout_batch_id, "accrued") if u.payout_batch_id else "accrued",
                   "at": u.created_at.isoformat()} for u in usage],
        "ledger": [{"kind": e.kind, "amount_cents": e.amount_cents, "details": e.details, "at": e.created_at.isoformat()}
                   for e in ledger],
        "payouts": batch_rows,
    }


@router.post("/wallet/topups")
def topup(body: TopupIn, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    p = create_topup(db, s.id, body.amount_cents)
    db.commit()
    return _purchase_view(db, p, detail=True)


@router.post("/wallet/settle")
def settle(s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    return {"payouts": [payouts.view(b) for b in payouts.settle(db, s.id)]}


@router.post("/wallet/payouts/{payout_id}/refresh")
def refresh_payout(payout_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    return payouts.view(payouts.refresh(db, s.id, payout_id))


# --- fulfilment and disputes (storefront / seller side) ---


def _shipment_view(s) -> dict:
    return {"tracking_number": s.tracking_number, "carrier": s.carrier_name_other or s.carrier,
            "shipped_at": s.shipped_at.isoformat(), "delivered_at": s.delivered_at.isoformat() if s.delivered_at else None,
            "tracker_state": s.tracker_state, "tracker_error": s.tracker_error}


@router.post("/purchases/{purchase_id}/ship")
def ship(purchase_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    return _shipment_view(disputes.ship(db, s.id, purchase_id))


@router.post("/purchases/{purchase_id}/deliver")
def deliver(purchase_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    return _shipment_view(disputes.mark_delivered(db, s.id, purchase_id))


@router.post("/purchases/{purchase_id}/simulate-dispute")
def simulate_dispute(purchase_id: str, body: SimulateDisputeIn, s: DemoSession = Depends(current_session),
                     db: Session = Depends(get_db)) -> dict:
    return disputes.view(disputes.simulate_dispute(db, s.id, purchase_id, body.reason))


@router.get("/disputes")
def list_disputes(s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Dispute).where(Dispute.session_id == s.id).order_by(Dispute.created_at.desc()))
    out = []
    for d in rows:
        v = disputes.view(d)
        sh = db.scalar(select(Shipment).where(Shipment.purchase_id == d.purchase_id)) if d.purchase_id else None
        v["shipment"] = _shipment_view(sh) if sh else None
        out.append(v)
    return {"disputes": out}


@router.post("/disputes/{dispute_id}/draft")
def draft_dispute(dispute_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    return disputes.view(disputes.request_draft(db, s.id, dispute_id))


@router.post("/disputes/{dispute_id}/approve")
def approve_dispute(dispute_id: str, body: DisputeApproveIn, s: DemoSession = Depends(current_session),
                    db: Session = Depends(get_db)) -> dict:
    return disputes.view(disputes.approve_and_submit(db, s.id, dispute_id, body.digest))


@router.post("/disputes/{dispute_id}/refresh")
def refresh_dispute(dispute_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    return disputes.view(disputes.refresh(db, s.id, dispute_id))


# --- analytics for the AG Studio dashboard (flat rows; dollars as display numbers, cents stay authoritative) ---


@router.get("/analytics")
def analytics(s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    suppliers = {x.id: x for x in db.scalars(select(Supplier).where(Supplier.session_id == s.id))}
    procs = list(db.scalars(select(Procurement).where(Procurement.session_id == s.id)))
    quotes = db.scalars(select(Quote).where(Quote.procurement_id.in_([p.id for p in procs])))
    offers = [{"supplier": suppliers[q.supplier_id].name.replace(" (simulated)", ""), "negotiated": "negotiated" if q.negotiated else "list price",
               "total_usd": q.total_cents / 100, "discount_usd": q.discount_cents / 100, "shipping_usd": q.shipping_cents / 100,
               "quote_status": q.status, "delivery_date": q.delivery_date.isoformat()} for q in quotes]
    proposals = db.scalars(select(Proposal).where(Proposal.session_id == s.id, Proposal.status.in_(["current", "approved"])))
    savings = [{"proposal_version": p.version, "supplier": p.snapshot["supplier"]["name"].replace(" (simulated)", ""),
                "total_usd": p.snapshot["total_cents"] / 100,
                "savings_usd": (p.comparison.get("savings_cents") or 0) / 100,
                "budget_headroom_usd": (p.snapshot["budget_cents"] - p.snapshot["total_cents"]) / 100, "status": p.status}
               for p in proposals]
    purchases_rows = [{"kind": p.kind.replace("_", " "), "status": p.status.replace("_", " "), "amount_usd": p.amount_cents / 100,
                       "counterparty": p.snapshot.get("supplier", {}).get("name", "").replace(" (simulated)", ""),
                       "day": p.created_at.date().isoformat()}
                      for p in db.scalars(select(Purchase).where(Purchase.session_id == s.id))]
    tools = {t.code: t for t in db.scalars(select(PaidTool))}
    usage = [{"tool": tools[u.tool_code].name, "provider": tools[u.tool_code].provider_name.replace(" (simulated)", ""),
              "cents": u.price_cents, "calls": 1, "settled": "settled" if u.payout_batch_id else "accrued"}
             for u in db.scalars(select(ToolUsage).where(ToolUsage.session_id == s.id))]
    dispute_rows = [{"reason": d.reason.replace("_", " ").lower(), "status": d.status.replace("_", " "), "amount_usd": d.amount_cents / 100}
                    for d in db.scalars(select(Dispute).where(Dispute.session_id == s.id))]
    inventory = [{"sku": line.sku, "category": line.category, "on_hand": line.on_hand, "incoming": line.incoming,
                  "need": line.need, "status": line.status}
                 for line in restock_lines(db, s.id, s.business_date, 14)]
    return {"sources": {"offers": offers, "savings": savings, "purchases": purchases_rows, "tool_usage": usage,
                        "disputes": dispute_rows, "inventory": inventory},
            "note": "Dollar values are display conversions of integer-cent records."}
