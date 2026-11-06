"""Seller-side dispute defense in fixture mode: shipment tracking, intake, cited drafts, approval, submission."""

import pytest
from sqlalchemy import func, select

from app import disputes
from app.config import get_settings
from app.jobs import drain
from app.models import FixtureProviderRecord
from app.payments import FixturePayPal
from tests.test_payments import approved_purchase, checkout


@pytest.fixture(autouse=True)
def no_faults():
    FixturePayPal.faults.clear()
    yield
    FixturePayPal.faults.clear()


def paid_purchase(api) -> str:
    p = approved_purchase(api)
    checkout(api, p["purchase_id"])
    assert api.post(f"/api/purchases/{p['purchase_id']}/capture").json()["status"] == "paid"
    return p["purchase_id"]


def open_dispute(api, pid, reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED") -> dict:
    r = api.post(f"/api/purchases/{pid}/simulate-dispute", {"reason": reason})
    assert r.status_code == 200, r.text
    return r.json()


def drafted(api, d) -> dict:
    assert api.post(f"/api/disputes/{d['dispute_id']}/draft").json()["status"] == "drafting"
    drain()
    return next(x for x in api.get("/api/disputes").json()["disputes"] if x["dispute_id"] == d["dispute_id"])


def test_full_defense_with_tracking_and_cited_evidence(api, db):
    pid = paid_purchase(api)
    shipped = api.post(f"/api/purchases/{pid}/ship").json()
    assert shipped["tracker_state"] == "pushed" and shipped["tracking_number"].startswith("TPDF")
    assert api.post(f"/api/purchases/{pid}/ship").json()["tracking_number"] == shipped["tracking_number"]
    api.post(f"/api/purchases/{pid}/deliver")

    d = open_dispute(api, pid)
    assert d["purchase_id"] == pid and d["status"] == "open" and d["amount_cents"] == 47_000
    d = drafted(api, d)
    assert d["status"] == "draft_ready" and d["draft"]["draft"]["recommendation"] == "contest"
    record_ids = {r["id"] for r in d["draft"]["records"]}
    assert all(set(c["citations"]) <= record_ids for c in d["draft"]["draft"]["claims"])
    evidence = d["draft"]["submission"][0]
    assert evidence["evidence_type"] == "PROOF_OF_FULFILLMENT"
    assert evidence["evidence_info"]["tracking_info"][0]["tracking_number"] == shipped["tracking_number"]

    stale = api.post(f"/api/disputes/{d['dispute_id']}/approve", {"digest": "0" * 64})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_draft"
    sent = api.post(f"/api/disputes/{d['dispute_id']}/approve", {"digest": d["draft_digest"]}).json()
    assert sent["status"] == "submitted" and sent["paypal_status"] == "UNDER_REVIEW"
    again = api.post(f"/api/disputes/{d['dispute_id']}/approve", {"digest": d["draft_digest"]}).json()
    assert again["status"] == "submitted"
    n = db.scalar(select(func.count()).select_from(FixtureProviderRecord).where(FixtureProviderRecord.kind == "evidence_request"))
    assert n == 1


def test_without_shipment_the_draft_recommends_accepting(api):
    d = drafted(api, open_dispute(api, paid_purchase(api)))
    assert d["draft"]["draft"]["recommendation"] == "accept"
    assert d["draft"]["submission"][0]["evidence_type"] == "OTHER"


def test_lost_evidence_response_is_reconciled_without_resending(api, db):
    pid = paid_purchase(api)
    api.post(f"/api/purchases/{pid}/ship")
    d = drafted(api, open_dispute(api, pid))
    FixturePayPal.faults.add("evidence_timeout_after_commit")
    lost = api.post(f"/api/disputes/{d['dispute_id']}/approve", {"digest": d["draft_digest"]}).json()
    assert lost["status"] == "unknown"
    fixed = api.post(f"/api/disputes/{d['dispute_id']}/refresh").json()
    assert fixed["status"] == "submitted"
    n = db.scalar(select(func.count()).select_from(FixtureProviderRecord).where(FixtureProviderRecord.kind == "evidence_request"))
    assert n == 1


def test_model_draft_must_cite_real_records(api, monkeypatch):
    d = open_dispute(api, paid_purchase(api))  # buyer run and payment stay in fixture mode
    monkeypatch.setattr(get_settings(), "ai_mode", "gemini")
    calls = []

    def fake(settings, system, prompt, schema, timeout_s=60.0):
        calls.append(prompt)
        if len(calls) == 1:  # invented record and amount: must be rejected and retried
            return {"recommendation": "contest", "summary": "Refund of $999.00 was issued.",
                    "claims": [{"text": "Shipped.", "citations": ["shipment:made-up"]}]}, {"model": "fake"}
        return {"recommendation": "contest", "summary": "The order was paid and approved as shown.",
                "claims": [{"text": "Buyer approved the $470.00 cart.", "citations": [r for r in prompt.split('"') if r.startswith("approval:")][:1]}]}, {"model": "fake"}

    monkeypatch.setattr(disputes, "generate_json", fake)
    d = drafted(api, d)
    assert len(calls) == 2 and "previous draft was rejected" in calls[1]
    assert d["status"] == "draft_ready" and d["draft"]["model"] == "fake"


def test_validation_rules():
    records = [{"id": "capture:C1", "type": "payment_capture", "amount": "470.00"}]
    assert disputes.validate_draft({"recommendation": "contest", "summary": "Paid $470.00.",
                                    "claims": [{"text": "Captured.", "citations": ["capture:C1"]}]}, records) == []
    problems = disputes.validate_draft({"recommendation": "contest", "summary": "Paid $12.00.",
                                        "claims": [{"text": "x", "citations": ["capture:NOPE"]}]}, records)
    assert any("unknown records" in p for p in problems) and any("$12.00" in p for p in problems)


def test_isolation_and_webhook_intake(api, other_api, db):
    pid = paid_purchase(api)
    d = open_dispute(api, pid)
    assert other_api.post(f"/api/disputes/{d['dispute_id']}/draft").status_code == 404
    assert other_api.get("/api/disputes").json()["disputes"] == []
    # A verified webhook for a second dispute is re-read from the provider and ingested.
    order_id = api.get(f"/api/purchases/{pid}").json()["paypal_order_id"]
    second = FixturePayPal().open_dispute(order_id, "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED")
    r = api.client.post("/api/webhooks/paypal", headers={"paypal-transmission-sig": "fixture-valid"},
                        json={"id": "WH-D1", "event_type": "CUSTOMER.DISPUTE.CREATED", "resource": {"dispute_id": second}})
    assert r.status_code == 200
    drain()
    ids = {x["paypal_dispute_id"] for x in api.get("/api/disputes").json()["disputes"]}
    assert second in ids
