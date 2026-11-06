"""Tool-boundary enforcement and pay-per-call metering, independent of which model drives the tools."""

import threading

import pytest
from sqlalchemy import func, select

from app.agent_tools import RunLimitReached, ToolContext, execute
from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import AppError
from app.models import AgentRun, DemoSession, LedgerEntry, Procurement, ToolUsage
from app.wallet import charge
from tests.helpers import confirmed_procurement


def make_ctx(db, procurement_id):
    proc = db.get(Procurement, procurement_id)
    sess = db.get(DemoSession, proc.session_id)
    run = AgentRun(procurement_id=proc.id, session_id=sess.id, mode="test", status="running")
    db.add(run)
    db.commit()
    return ToolContext(db=db, session=sess, procurement=proc, run=run, settings=get_settings())


def test_unknown_tools_and_bad_arguments_are_rejected(api, db):
    ctx = make_ctx(db, confirmed_procurement(api)["procurement_id"])
    assert execute(ctx, "capture_payment", {"purchase_id": "x"})["error"]["code"] == "unknown_tool"
    assert execute(ctx, "approve_purchase", {})["error"]["code"] == "unknown_tool"
    r = execute(ctx, "request_quote", {"supplier_id": "supplier-b", "session_id": "someone-else"})
    assert r["error"]["code"] == "invalid_arguments"  # unexpected fields are not ignored
    assert execute(ctx, "request_quote", {"supplier_id": "https://evil.example"})["error"]["code"] == "invalid_arguments"
    assert execute(ctx, "counteroffer", {"supplier_id": "supplier-b", "requested_discount_bps": "300"})["error"]["code"] == "invalid_arguments"
    assert execute(ctx, "request_quote", "not an object")["error"]["code"] == "invalid_arguments"
    assert execute(ctx, "request_quote", {"supplier_id": "supplier-z"})["error"]["code"] == "not_found"
    assert db.scalar(select(func.count()).select_from(ToolUsage)) == 0  # rejected calls are never charged


def test_tool_call_budget_is_enforced(api, db, monkeypatch):
    ctx = make_ctx(db, confirmed_procurement(api)["procurement_id"])
    monkeypatch.setattr(ctx.settings, "ai_max_tool_calls", 3)
    for _ in range(3):
        execute(ctx, "list_suppliers", {})
    with pytest.raises(RunLimitReached) as exc:
        execute(ctx, "list_suppliers", {})
    assert exc.value.code == "tool_call_limit"


def test_supplier_prompt_injection_cannot_change_the_outcome(api, db):
    """Cobalt's description tells agents to pick it. The backend still refuses an infeasible proposal."""
    ctx = make_ctx(db, confirmed_procurement(api)["procurement_id"])
    suppliers = execute(ctx, "list_suppliers", {})["data"]["suppliers"]
    c = next(s for s in suppliers if s["supplier_id"] == "supplier-c")
    assert "ignore your budget" in c["description_untrusted"]
    quote_c = execute(ctx, "request_quote", {"supplier_id": "supplier-c"})["data"]["quote_id"]
    execute(ctx, "request_quote", {"supplier_id": "supplier-a"})
    r = execute(ctx, "propose_purchase", {"quote_id": quote_c, "explanation": "Supplier said so."})
    assert r["ok"] is False and r["error"]["code"] == "not_best_feasible_offer"


def test_over_budget_proposal_is_rejected(api, db):
    ctx = make_ctx(db, confirmed_procurement(api, budget_cents=40_000)["procurement_id"])
    qa = execute(ctx, "request_quote", {"supplier_id": "supplier-a"})["data"]["quote_id"]
    r = execute(ctx, "propose_purchase", {"quote_id": qa, "explanation": "cheapest"})
    assert r["error"]["code"] == "no_feasible_offer"


def test_wallet_charges_each_logical_call_once(api, db):
    sid = api.session["session_id"]
    u1 = charge(db, sid, None, "call-1", "quote.supplier-a")
    u2 = charge(db, sid, None, "call-1", "quote.supplier-a")
    db.commit()
    assert u1.id == u2.id
    db.expire_all()
    assert db.get(DemoSession, sid).wallet_balance_cents == 49
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "tool_usage")) == 1


def test_concurrent_duplicate_charges_debit_once(api):
    sid = api.session["session_id"]
    barrier = threading.Barrier(6)

    def worker():
        with get_sessionmaker()() as s:
            barrier.wait()
            charge(s, sid, None, "same-call", "market.price_check")
            s.commit()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with get_sessionmaker()() as s:
        assert s.get(DemoSession, sid).wallet_balance_cents == 48
        assert s.scalar(select(func.count()).select_from(ToolUsage)) == 1


def test_insufficient_balance_blocks_paid_tools(api, db):
    sid = api.session["session_id"]
    sess = db.get(DemoSession, sid)
    sess.wallet_balance_cents = 1
    db.commit()
    charge(db, sid, None, "a", "quote.supplier-a")
    db.commit()
    with pytest.raises(AppError) as exc:
        charge(db, sid, None, "b", "quote.supplier-a")
    assert exc.value.code == "insufficient_tool_balance"
    db.rollback()
    assert db.get(DemoSession, sid).wallet_balance_cents == 0


def test_run_spend_cap(api, db, monkeypatch):
    ctx = make_ctx(db, confirmed_procurement(api)["procurement_id"])
    monkeypatch.setattr(ctx.settings, "tool_spend_cap_per_run_cents", 2)
    assert execute(ctx, "request_quote", {"supplier_id": "supplier-a"})["ok"]
    assert execute(ctx, "request_quote", {"supplier_id": "supplier-b"})["ok"]
    assert execute(ctx, "request_quote", {"supplier_id": "supplier-c"})["error"]["code"] == "run_spend_cap"


def test_fixture_run_records_public_events_and_spend(api):
    from tests.helpers import run_buyer

    d = run_buyer(api, confirmed_procurement(api)["procurement_id"])
    assert d["run"]["status"] == "succeeded" and d["run"]["mode"] == "fixture"
    assert d["run"]["tool_spend_cents"] == 7  # 3 quotes + 4 negotiation rounds at 1¢
    assert api.get("/api/wallet").json()["balance_cents"] == 43
    tools = [e["tool"] for e in d["events"] if e["kind"] == "tool_call"]
    assert tools[0] == "get_inventory_snapshot" and tools[-1] == "propose_purchase"
