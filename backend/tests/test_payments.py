"""Fixture-mode checkout: create, simulated buyer approval, capture, finalizer idempotency, and recovery.

Fault-injection tests make the offline adapter commit remotely and then raise, standing in for a lost
response. They are labeled as such; they are not evidence of real PayPal behavior.
"""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.approvals import expire_reservations
from app.jobs import drain
from app.models import (
    Capture,
    FixtureProviderRecord,
    LedgerEntry,
    Product,
    Purchase,
    Reservation,
    SupplierItem,
)
from app.payments import FixturePayPal, finalize_from_order
from app.suppliers import utcnow
from tests.helpers import BASKET, approve_body, confirmed_procurement, run_buyer


@pytest.fixture(autouse=True)
def no_faults():
    FixturePayPal.faults.clear()
    yield
    FixturePayPal.faults.clear()


def approved_purchase(api) -> dict:
    proposal = run_buyer(api, confirmed_procurement(api)["procurement_id"])["proposal"]
    r = api.post(f"/api/proposals/{proposal['proposal_id']}/approve", approve_body(proposal))
    assert r.status_code == 200, r.text
    return r.json()


def checkout(api, pid: str) -> dict:
    r = api.post(f"/api/purchases/{pid}/paypal-order")
    assert r.status_code == 200, r.text
    r = api.post(f"/api/purchases/{pid}/fixture-approve")
    assert r.status_code == 200, r.text
    return r.json()


def incoming(db, session_id: str) -> dict[str, int]:
    db.expire_all()
    return {p.sku: p.incoming for p in db.scalars(select(Product).where(Product.session_id == session_id))}


def test_happy_path_pays_once_and_raises_incoming_once(api, db):
    before = incoming(db, api.session["session_id"])
    p = approved_purchase(api)
    assert p["status"] == "creating"
    item_id = p["reservation"]["lines"][0]["supplier_item_id"]
    stock_before = db.get(SupplierItem, item_id).stock
    order = api.post(f"/api/purchases/{p['purchase_id']}/paypal-order").json()
    assert order["status"] == "awaiting_paypal_approval" and order["paypal_order_id"].startswith("FX")
    # Repeating order creation returns the same order.
    assert api.post(f"/api/purchases/{p['purchase_id']}/paypal-order").json()["paypal_order_id"] == order["paypal_order_id"]

    api.post(f"/api/purchases/{p['purchase_id']}/fixture-approve")
    paid = api.post(f"/api/purchases/{p['purchase_id']}/capture").json()
    assert paid["status"] == "paid" and paid["capture"]["amount_cents"] == 47_000
    again = api.post(f"/api/purchases/{p['purchase_id']}/capture").json()
    assert again["capture_id"] == paid["capture_id"]

    after = incoming(db, api.session["session_id"])
    assert {sku: after[sku] - before[sku] for sku in BASKET} == BASKET
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "purchase_paid")) == 1
    res = db.scalar(select(Reservation).where(Reservation.purchase_id == p["purchase_id"]))
    assert res.status == "consumed"
    item = db.get(SupplierItem, item_id)
    assert item.reserved == 0 and item.stock == stock_before - p["reservation"]["lines"][0]["qty"]


def test_capture_before_buyer_approval_is_refused_without_state_change(api):
    p = approved_purchase(api)
    api.post(f"/api/purchases/{p['purchase_id']}/paypal-order")
    r = api.post(f"/api/purchases/{p['purchase_id']}/capture")
    assert r.status_code == 409 and r.json()["error"]["code"] == "order_not_approved"
    assert api.get(f"/api/purchases/{p['purchase_id']}").json()["status"] == "awaiting_paypal_approval"


def test_concurrent_capture_clicks_make_one_financial_effect(api, db):
    p = approved_purchase(api)
    checkout(api, p["purchase_id"])
    codes = []
    barrier = threading.Barrier(4)

    def click():
        barrier.wait()
        codes.append(api.post(f"/api/purchases/{p['purchase_id']}/capture").status_code)

    threads = [threading.Thread(target=click) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert 200 in codes
    assert db.scalar(select(func.count()).select_from(Capture)) == 1
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "purchase_paid")) == 1
    assert api.get(f"/api/purchases/{p['purchase_id']}").json()["status"] == "paid"


def test_lost_capture_response_pins_stock_then_reconciles_once(api, db):
    """Fault injection: the fixture captures remotely, then the response is 'lost'."""
    p = approved_purchase(api)
    checkout(api, p["purchase_id"])
    FixturePayPal.faults.add("capture_timeout_after_commit")
    r = api.post(f"/api/purchases/{p['purchase_id']}/capture")
    assert r.status_code == 502 and r.json()["error"]["code"] == "payment_status_unknown"
    view = api.get(f"/api/purchases/{p['purchase_id']}").json()
    assert view["status"] == "unknown" and view["reservation"]["status"] == "capture_in_progress"

    # TTL cleanup must not free stock while the capture outcome is unknown.
    assert expire_reservations(db, now=utcnow() + timedelta(hours=2)) == 0
    db.commit()

    drain()  # the scheduled reconcile job finds the completed capture
    view = api.get(f"/api/purchases/{p['purchase_id']}").json()
    assert view["status"] == "paid" and view["capture"]["source"] == "reconciliation"
    assert [e["kind"] for e in view["events"]].count("paid") == 1
    # A second reconcile or a replayed capture changes nothing.
    api.post(f"/api/purchases/{p['purchase_id']}/capture")
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "purchase_paid")) == 1


def test_lost_create_response_replays_same_request_id(api, db):
    """Fault injection: the order is created remotely but its ID never reaches us."""
    p = approved_purchase(api)
    FixturePayPal.faults.add("create_timeout_after_commit")
    r = api.post(f"/api/purchases/{p['purchase_id']}/paypal-order")
    assert r.status_code == 502
    assert api.get(f"/api/purchases/{p['purchase_id']}").json()["status"] == "unknown"
    drain()
    view = api.get(f"/api/purchases/{p['purchase_id']}").json()
    assert view["status"] == "awaiting_paypal_approval" and view["paypal_order_id"]
    orders = db.scalar(select(func.count()).select_from(FixtureProviderRecord).where(FixtureProviderRecord.kind == "order"))
    assert orders == 1  # no second order was created at the provider
    assert view["operations"][0]["attempts"] == 1


def test_duplicate_finalization_is_a_no_op(api, db):
    p = approved_purchase(api)
    paid = checkout(api, p["purchase_id"])
    api.post(f"/api/purchases/{p['purchase_id']}/capture")
    order = FixturePayPal().get_order(paid["paypal_order_id"])
    before = incoming(db, api.session["session_id"])
    for source in ("webhook", "webhook", "reconciliation"):
        finalize_from_order(db, p["purchase_id"], order, source)
    assert incoming(db, api.session["session_id"]) == before
    assert db.scalar(select(func.count()).select_from(Capture)) == 1


def test_capture_amount_mismatch_goes_to_manual_review(api, db):
    p = approved_purchase(api)
    view = checkout(api, p["purchase_id"])
    order = FixturePayPal().get_order(view["paypal_order_id"])
    order["status"] = "COMPLETED"
    order["purchase_units"][0]["payments"] = {"captures": [
        {"id": "FXCAPWRONG", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "1.00"}}
    ]}
    finalize_from_order(db, p["purchase_id"], order, "webhook")
    db.expire_all()
    purchase = db.get(Purchase, p["purchase_id"])
    assert purchase.status == "manual_review" and purchase.status_reason == "capture_amount_mismatch"
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "purchase_paid")) == 0


def test_other_session_cannot_touch_purchase(api, other_api):
    p = approved_purchase(api)
    for path in ("paypal-order", "fixture-approve", "capture", "reconcile"):
        assert other_api.post(f"/api/purchases/{p['purchase_id']}/{path}").status_code == 404
    assert other_api.get(f"/api/purchases/{p['purchase_id']}/evidence").status_code == 404


def test_expired_reservation_blocks_order_creation_and_releases_stock(api, db):
    p = approved_purchase(api)
    res = db.scalar(select(Reservation).where(Reservation.purchase_id == p["purchase_id"]))
    res.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    r = api.post(f"/api/purchases/{p['purchase_id']}/paypal-order")
    assert r.status_code == 409 and r.json()["error"]["code"] == "reservation_expired"
    db.expire_all()
    assert db.get(Reservation, res.id).status == "released"
    assert db.get(Purchase, p["purchase_id"]).status == "cancelled"


def test_evidence_downloads(api):
    p = approved_purchase(api)
    checkout(api, p["purchase_id"])
    api.post(f"/api/purchases/{p['purchase_id']}/capture")
    j = api.get(f"/api/purchases/{p['purchase_id']}/evidence")
    assert j.status_code == 200 and j.json()["purchase"]["status"] == "paid"
    c = api.get(f"/api/purchases/{p['purchase_id']}/evidence?format=csv")
    assert c.status_code == 200 and c.text.startswith("purchase_id,status,paypal_order_id")
    assert len(c.text.strip().splitlines()) == 1 + len(BASKET)
