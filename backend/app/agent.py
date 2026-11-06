"""Bounded buyer-agent runs. Fixture mode drives the real tools with a deterministic script; anthropic mode
lets Claude choose tool calls (see `anthropic_driver`). Both share `agent_tools.execute` and its limits."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_tools import RunLimitReached, ToolContext, execute
from app.config import get_settings
from app.errors import AppError
from app.jobs import enqueue, handler
from app.models import PROPOSAL_TRANSITIONS, AgentRun, DemoSession, Procurement, Proposal, transition
from app.procurement import set_procurement_status

RUNNABLE = {"draft", "no_feasible_offer", "failed", "awaiting_approval"}


def start_run(db: Session, session: DemoSession, procurement: Procurement) -> AgentRun:
    settings = get_settings()
    if procurement.requirements is None or procurement.status == "needs_input":
        raise AppError("requirements_not_confirmed", "Confirm requirements before the buyer contacts suppliers.", 409)
    if procurement.status not in RUNNABLE:
        raise AppError("run_not_allowed", f"Cannot start a run while the procurement is {procurement.status}.", 409)
    since = datetime.now(UTC) - timedelta(days=1)
    runs_today = db.scalar(
        select(func.count()).select_from(AgentRun).where(AgentRun.session_id == session.id, AgentRun.created_at >= since)
    )
    if runs_today >= settings.max_runs_per_session_per_day:
        raise AppError("rate_limited", "Daily buyer-run limit reached for this demo session.", 429)
    for prop in db.scalars(select(Proposal).where(Proposal.procurement_id == procurement.id, Proposal.status == "current")):
        transition(prop, "superseded", PROPOSAL_TRANSITIONS, "proposal")
        prop.status_reason = "new_run_started"
    run = AgentRun(
        procurement_id=procurement.id,
        session_id=session.id,
        mode=settings.ai_mode,
        model=settings.anthropic_model if settings.ai_mode == "anthropic" else "fixture-script",
    )
    db.add(run)
    set_procurement_status(procurement, "planning", None)
    db.flush()
    enqueue(db, "agent_run", {"run_id": run.id}, dedupe_key=f"agent_run:{run.id}")
    return run


def _money(c: int) -> str:
    return f"${c / 100:,.2f}"


def fixture_explanation(comparison: dict) -> str:
    """Deterministic template over comparison facts (fixture mode). No model wrote this."""
    best = next(o for o in comparison["offers"] if o["quote_id"] == comparison["best_quote_id"])
    parts = [
        (
            f"Recommend {best['supplier_name']}: {_money(best['total_cents'])} delivered by {best['delivery_date']}, "
            f"within the {_money(comparison['budget_cents'])} budget and the {comparison['deadline']} deadline."
        )
    ]
    if best["discount_cents"]:
        parts.append(f"Includes a negotiated {best['discount_bps'] / 100:.2f}% discount ({_money(best['discount_cents'])}).")
    if best["uses_surplus"]:
        parts.append("Uses a compatible surplus item that meets or exceeds every required specification.")
    if comparison.get("baseline") and comparison.get("savings_cents"):
        parts.append(
            f"Saves {_money(comparison['savings_cents'])} against the best current list-price basket "
            f"({comparison['baseline']['supplier_id']}, {_money(comparison['baseline']['total_cents'])})."
        )
    for o in comparison["offers"]:
        if not o["feasible"]:
            parts.append(f"Rejected {o['supplier_name']}: {o['reasons'][0]['message']}.")
    return " ".join(parts)


def fixture_driver(ctx: ToolContext) -> str:
    """A fixed procurement strategy that calls the same validated tools a model would."""
    execute(ctx, "get_inventory_snapshot", {})
    execute(ctx, "calculate_restock_needs", {})
    suppliers = execute(ctx, "list_suppliers", {})["data"]["suppliers"]
    for s in suppliers:
        execute(ctx, "request_quote", {"supplier_id": s["supplier_id"]})
    for s in suppliers:
        if not s["negotiable"]:
            continue
        offers = execute(ctx, "get_compatible_surplus_offers", {"supplier_id": s["supplier_id"]})["data"]["offers"]
        compatible = [o["surplus_sku"] for o in offers if o["compatible"] and o["unit_cents"] > 0]
        r1 = execute(ctx, "counteroffer", {"supplier_id": s["supplier_id"], "requested_discount_bps": 300,
                                           "surplus_skus": compatible})
        if r1["ok"] and r1["data"]["outcome"] == "countered":
            ask = min(r1["data"]["granted_discount_bps"] + 100, 300)
            execute(ctx, "counteroffer", {"supplier_id": s["supplier_id"], "requested_discount_bps": ask, "surplus_skus": []})
    comparison = execute(ctx, "compare_offers", {})["data"]
    if comparison["best_quote_id"]:
        execute(ctx, "propose_purchase", {"quote_id": comparison["best_quote_id"],
                                          "explanation": fixture_explanation(comparison)})
        return fixture_explanation(comparison)
    return "No feasible offer. " + " ".join(comparison["suggestions"])


def anthropic_driver(ctx: ToolContext) -> str:
    from app.agent_llm import run_claude

    return run_claude(ctx)


@handler("agent_run")
def run_agent_job(db: Session, payload: dict) -> None:
    settings = get_settings()
    run = db.get(AgentRun, payload["run_id"])
    if run is None or run.status in ("succeeded", "failed"):
        return
    procurement = db.get(Procurement, run.procurement_id)
    session = db.get(DemoSession, run.session_id)
    if run.status == "running":
        # The worker died mid-run. Tool calls already made stay recorded; the owner can start a new run.
        _finish(db, run, procurement, "failed", "interrupted", "The run was interrupted by a restart. Start a new run.")
        return
    run.status, run.started_at = "running", datetime.now(UTC)
    db.commit()
    ctx = ToolContext(db=db, session=session, procurement=procurement, run=run, settings=settings)
    ctx.event("status", f"Run started ({run.mode}{', model ' + run.model if run.model else ''})")
    try:
        summary = fixture_driver(ctx) if run.mode == "fixture" else anthropic_driver(ctx)
    except RunLimitReached as exc:
        db.rollback()
        ctx.event("error", exc.args[0], ok=False)
        _finish(db, run, procurement, "failed", exc.code, exc.args[0])
        return
    except AppError as exc:
        db.rollback()
        ctx.event("error", exc.message, ok=False)
        _finish(db, run, procurement, "failed", exc.code, exc.message)
        return
    db.refresh(procurement)
    if ctx.proposal_id:
        _finish(db, run, procurement, "succeeded", None, None, summary)
    else:
        if procurement.status in ("planning", "negotiating"):
            set_procurement_status(procurement, "no_feasible_offer", "no_feasible_offer")
        _finish(db, run, procurement, "succeeded", None, None, summary)


def _finish(db: Session, run: AgentRun, procurement: Procurement, status: str, code: str | None,
            message: str | None, summary: str | None = None) -> None:
    run.status, run.error_code, run.error_message = status, code, message
    run.summary = summary
    run.finished_at = datetime.now(UTC)
    if status == "failed" and procurement.status in ("planning", "negotiating"):
        set_procurement_status(procurement, "failed", code)
    db.commit()
