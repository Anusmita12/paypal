from sqlalchemy import func, select

from app.jobs import drain
from app.models import LedgerEntry, WebhookEvent
from app.payments import FixturePayPal
from tests.test_payments import approved_purchase, checkout

SIG = {"paypal-transmission-sig": "fixture-valid"}


def capture_event(order_id: str, event_id: str = "WH-1") -> dict:
    return {"id": event_id, "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "CAPX", "status": "COMPLETED",
                         "supplementary_data": {"related_ids": {"order_id": order_id}}}}


def test_webhook_before_browser_response_finalizes_once(api, db):
    p = approved_purchase(api)
    view = checkout(api, p["purchase_id"])
    # PayPal captured, but our browser/server response never arrived; only the webhook does.
    FixturePayPal().capture_order(view["paypal_order_id"], "external-capture")
    r = api.client.post("/api/webhooks/paypal", json=capture_event(view["paypal_order_id"]), headers=SIG)
    assert r.status_code == 200 and r.json() == {"status": "accepted"}
    again = api.client.post("/api/webhooks/paypal", json=capture_event(view["paypal_order_id"]), headers=SIG)
    assert again.json() == {"status": "duplicate"}
    drain()
    paid = api.get(f"/api/purchases/{p['purchase_id']}").json()
    assert paid["status"] == "paid" and paid["capture"]["source"] == "webhook"
    # The late browser capture click is now a no-op.
    assert api.post(f"/api/purchases/{p['purchase_id']}/capture").json()["status"] == "paid"
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "purchase_paid")) == 1
    ev = db.scalar(select(WebhookEvent))
    assert ev.status == "processed" and ev.delivery_count == 2


def test_invalid_signature_and_garbage_are_rejected(api, db):
    r = api.client.post("/api/webhooks/paypal", json=capture_event("FXNOPE"), headers={"paypal-transmission-sig": "forged"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_signature"
    assert api.client.post("/api/webhooks/paypal", content=b"not json", headers=SIG).status_code == 400
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_unknown_event_is_recorded_as_ignored(api, db):
    r = api.client.post("/api/webhooks/paypal", json={"id": "WH-9", "event_type": "BILLING.PLAN.CREATED", "resource": {}},
                        headers=SIG)
    assert r.status_code == 200
    drain()
    assert db.scalar(select(WebhookEvent)).status == "ignored"
