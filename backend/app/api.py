from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import approvals
from app.agent import start_run
from app.compare import compare
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError, not_found
from app.models import (
    AgentEvent,
    AgentRun,
    DemoSession,
    LedgerEntry,
    NegotiationRound,
    PaidTool,
    Procurement,
    Proposal,
    Purchase,
    Quote,
    Reservation,
    Supplier,
    ToolUsage,
)
from app.procurement import confirm_requirements, create_procurement, get_procurement
from app.restock import FORMULA, WINDOW_DAYS, restock_lines, window
from app.schemas import ApproveIn, ConfirmIn, CreateProcurementIn
from app.security import create_session, current_session
from app.suppliers import apply_scenario

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
        "anthropic_model": settings.anthropic_model if settings.ai_mode == "anthropic" else None,
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


# --- purchases and wallet ---


def _purchase_view(db: Session, p: Purchase) -> dict:
    res = db.scalar(select(Reservation).where(Reservation.purchase_id == p.id))
    return {
        "purchase_id": p.id,
        "kind": p.kind,
        "status": p.status,
        "status_reason": p.status_reason,
        "amount_cents": p.amount_cents,
        "currency": p.currency,
        "snapshot": p.snapshot,
        "digest": p.digest,
        "paypal_order_id": p.paypal_order_id,
        "payment_mode": p.payment_mode,
        "reservation": res and {"status": res.status, "expires_at": res.expires_at.isoformat(), "lines": res.lines},
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


@router.get("/purchases")
def purchases(s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Purchase).where(Purchase.session_id == s.id).order_by(Purchase.created_at.desc()))
    return {"purchases": [_purchase_view(db, p) for p in rows]}


@router.get("/purchases/{purchase_id}")
def purchase_detail(purchase_id: str, s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    p = db.scalar(select(Purchase).where(Purchase.id == purchase_id, Purchase.session_id == s.id))
    if not p:
        raise not_found("purchase")
    return _purchase_view(db, p)


@router.get("/wallet")
def wallet(s: DemoSession = Depends(current_session), db: Session = Depends(get_db)) -> dict:
    tools = {t.code: t for t in db.scalars(select(PaidTool))}
    usage = db.scalars(select(ToolUsage).where(ToolUsage.session_id == s.id).order_by(ToolUsage.created_at.desc()).limit(500))
    ledger = db.scalars(select(LedgerEntry).where(LedgerEntry.session_id == s.id).order_by(LedgerEntry.created_at.desc()).limit(200))
    return {
        "balance_cents": s.wallet_balance_cents,
        "tools": [{"code": t.code, "name": t.name, "provider_id": t.provider_id, "provider_name": t.provider_name,
                   "price_cents": t.price_cents} for t in tools.values()],
        "usage": [{"id": u.id, "run_id": u.run_id, "tool_code": u.tool_code, "tool_name": tools[u.tool_code].name,
                   "provider_id": u.provider_id, "price_cents": u.price_cents, "settled": u.payout_batch_id is not None,
                   "at": u.created_at.isoformat()} for u in usage],
        "ledger": [{"kind": e.kind, "amount_cents": e.amount_cents, "details": e.details, "at": e.created_at.isoformat()}
                   for e in ledger],
    }
