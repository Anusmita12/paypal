"""Tool-credit top-up through the shared checkout, and provider settlement through Payouts (fixture provider)."""

import pytest
from sqlalchemy import func, select

from app.models import FixtureProviderRecord, LedgerEntry, PayoutBatch, ToolUsage
from app.payments import FixturePayPal
from tests.helpers import confirmed_procurement, run_buyer


@pytest.fixture(autouse=True)
def no_faults():
    FixturePayPal.faults.clear()
    yield
    FixturePayPal.faults.clear()


def pay_topup(api, cents=500):
    t = api.post("/api/wallet/topups", {"amount_cents": cents})
    assert t.status_code == 200, t.text
    pid = t.json()["purchase_id"]
    assert api.post(f"/api/purchases/{pid}/paypal-order").json()["status"] == "awaiting_paypal_approval"
    api.post(f"/api/purchases/{pid}/fixture-approve")
    return api.post(f"/api/purchases/{pid}/capture").json()


def test_topup_credits_wallet_exactly_once(api, db):
    paid = pay_topup(api)
    assert paid["status"] == "paid" and paid["kind"] == "wallet_topup"
    assert paid["reservation"] is None
    for _ in range(2):  # repeated capture / reconcile do nothing more
        api.post(f"/api/purchases/{paid['purchase_id']}/capture")
    assert api.get("/api/wallet").json()["balance_cents"] == 50 + 500
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "wallet_topup")) == 1


def test_topup_amount_is_server_controlled(api):
    for bad in (1, 499, 10_000):
        r = api.post("/api/wallet/topups", {"amount_cents": bad})
        assert r.status_code == 422
    assert api.post("/api/wallet/topups", {"amount_cents": 500, "currency": "EUR"}).status_code == 422


def test_settlement_pays_each_provider_once(api, db):
    run_buyer(api, confirmed_procurement(api)["procurement_id"])  # 9¢ of tool calls across 4 providers
    r = api.post("/api/wallet/settle")
    assert r.status_code == 200
    batches = {b["provider_id"]: b for b in r.json()["payouts"]}
    assert {k: v["amount_cents"] for k, v in batches.items()} == {
        "market-desk": 2, "supplier-a": 1, "supplier-b": 3, "supplier-c": 3}
    assert all(b["status"] == "submitted" and b["paypal_batch_id"].startswith("FXPO") for b in batches.values())
    # Nothing is left to settle; a second click cannot pay again.
    again = api.post("/api/wallet/settle")
    assert again.status_code == 409 and again.json()["error"]["code"] == "nothing_to_settle"
    refreshed = api.post(f"/api/wallet/payouts/{batches['supplier-b']['payout_id']}/refresh").json()
    assert refreshed["status"] == "success"
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "provider_payout")) == 1
    w = api.get("/api/wallet").json()
    assert w["unsettled_cents"] == 0
    assert {u["payout_status"] for u in w["usage"] if u["provider_id"] == "supplier-b"} == {"success"}


def test_lost_payout_response_replays_same_batch_id(api, db):
    """Fault injection: PayPal accepted the batch but the response was lost."""
    run_buyer(api, confirmed_procurement(api)["procurement_id"])
    FixturePayPal.faults.add("payout_timeout_after_commit")
    batches = api.post("/api/wallet/settle").json()["payouts"]
    lost = next(b for b in batches if b["status"] == "unknown")
    assert lost["paypal_batch_id"] is None
    recovered = api.post(f"/api/wallet/payouts/{lost['payout_id']}/refresh").json()
    assert recovered["status"] == "submitted" and recovered["paypal_batch_id"]
    assert recovered["sender_batch_id"] == lost["sender_batch_id"] and recovered["attempts"] == 2
    created = db.scalar(select(func.count()).select_from(FixtureProviderRecord).where(FixtureProviderRecord.kind == "payout"))
    assert created == len(batches)  # no extra batch at the provider


def test_other_session_cannot_refresh_payouts(api, other_api, db):
    run_buyer(api, confirmed_procurement(api)["procurement_id"])
    b = api.post("/api/wallet/settle").json()["payouts"][0]
    assert other_api.post(f"/api/wallet/payouts/{b['payout_id']}/refresh").status_code == 404
    assert db.scalar(select(func.count()).select_from(PayoutBatch)) == 4
    assert db.scalar(select(func.count()).select_from(ToolUsage).where(ToolUsage.payout_batch_id.is_(None))) == 0
