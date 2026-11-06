"""Main fixture story and supplier policy, driven through the public API and the real domain code."""

from sqlalchemy import select

from app.models import Quote, SupplierItem
from app.restock import compute_need
from tests.helpers import BASKET, approve_body, confirmed_procurement, run_buyer


def line_total(quote_lines):
    return sum(line["qty"] * line["unit_cents"] for line in quote_lines)


def test_restock_formula_rounding():
    # 24 units / 28 days * 14 = 12 exactly; 25 units -> 12.5 -> ceil 13
    assert compute_need(24, 14, 4, 6, 0) == (16, 10)
    assert compute_need(25, 14, 4, 6, 0) == (17, 11)
    assert compute_need(0, 14, 2, 5, 0) == (2, 0)  # never negative
    assert compute_need(20, 14, 4, 5, 6) == (14, 3)  # incoming counts against need


def test_seeded_inventory_has_designed_shortages(api):
    inv = api.get("/api/inventory").json()
    needs = {p["sku"]: p["need"] for p in inv["products"] if p["need"]}
    assert needs == BASKET
    hub = next(p for p in inv["products"] if p["sku"] == "HUB-C-7IN1")
    assert "10 of 28 days" in hub["warning"]
    assert inv["window"] == {"start": "2026-10-07", "end": "2026-11-03", "days": 28}


def test_main_story_totals_add_up_from_line_items(api):
    p = confirmed_procurement(api)
    first = run_buyer(api, p["procurement_id"])
    offers = {o["supplier_id"]: o for o in first["comparison"]["offers"]}
    quotes = {q["quote_id"]: q for q in first["quotes"]}

    # Initial list-price quotes, recomputed from line items.
    initial = {q["supplier_id"]: q for q in first["quotes"] if not q["negotiated"]}
    assert line_total(initial["supplier-a"]["lines"]) + 1000 == 47_000
    assert line_total(initial["supplier-b"]["lines"]) + 2250 == 51_000
    assert line_total(initial["supplier-c"]["lines"]) + 0 == 45_500

    # B negotiated: compatible surplus charger + 2% floor discount = $488.00.
    b = quotes[offers["supplier-b"]["quote_id"]]
    sub = line_total(b["lines"])
    assert sub == 47_500
    assert sub - sub * 200 // 10_000 + 2250 == offers["supplier-b"]["total_cents"] == 48_800
    assert any(line["offered_sku"] == "SUR-CHG-65W-GAN" for line in b["lines"])

    # C is cheaper but rejected for a specific spec failure.
    assert not offers["supplier-c"]["feasible"]
    assert [r["code"] for r in offers["supplier-c"]["reasons"]] == ["spec_mismatch"]
    assert "mfi" in offers["supplier-c"]["reasons"][0]["message"]

    # A wins before the disruption.
    assert first["proposal"]["snapshot"]["supplier"]["id"] == "supplier-a"
    assert first["proposal"]["snapshot"]["total_cents"] == 47_000

    # Disruption: A revises prices; the old proposal cannot be approved.
    api.post("/api/demo/scenarios/supplier_a_price_increase")
    r = api.post(f"/api/proposals/{first['proposal']['proposal_id']}/approve", approve_body(first["proposal"]))
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "supplier_terms_changed"

    second = run_buyer(api, p["procurement_id"])
    offers = {o["supplier_id"]: o for o in second["comparison"]["offers"]}
    assert offers["supplier-a"]["total_cents"] == 52_500
    assert [r["code"] for r in offers["supplier-a"]["reasons"]] == ["over_budget"]
    snap = second["proposal"]["snapshot"]
    assert snap["supplier"]["id"] == "supplier-b" and snap["total_cents"] == 48_800
    assert second["comparison"]["baseline"]["total_cents"] == 51_000
    assert second["comparison"]["savings_cents"] == 2_200
    assert snap["subtotal_cents"] - snap["discount_cents"] + snap["shipping_cents"] + snap["tax_cents"] == snap["total_cents"]


def test_discount_floor_and_round_limit(api, db):
    from app.models import DemoSession, Procurement
    from app.suppliers import build_quote, negotiate, supplier_by_code

    p = confirmed_procurement(api)
    proc = db.get(Procurement, p["procurement_id"])
    sess = db.get(DemoSession, proc.session_id)
    b = supplier_by_code(db, sess.id, "supplier-b")
    q = build_quote(db, proc, b)
    granted = []
    for _ in range(3):
        rnd, newq = negotiate(db, proc, q, 5000, [])
        granted.append(rnd.granted_discount_bps)
        q = newq or q
    assert granted == [100, 200, 200]  # ladder, then the floor; never beyond max_discount_bps
    import pytest

    from app.errors import AppError

    with pytest.raises(AppError) as exc:
        negotiate(db, proc, q, 100, [])
    assert exc.value.code == "negotiation_limit"


def test_incompatible_surplus_is_rejected(api, db):
    from app.models import DemoSession, NegotiationRound, Procurement
    from app.suppliers import build_quote, negotiate, supplier_by_code, surplus_offers

    p = confirmed_procurement(api)
    proc = db.get(Procurement, p["procurement_id"])
    b = supplier_by_code(db, db.get(DemoSession, proc.session_id).id, "supplier-b")
    offers = {o["surplus_sku"]: o for o in surplus_offers(db, proc, b)}
    assert offers["SUR-CHG-65W-GAN"]["compatible"]
    assert not offers["SUR-CBL-C2C-1M-30W"]["compatible"]
    assert "power_w 30 < required 60" in offers["SUR-CBL-C2C-1M-30W"]["compatibility_problems"]

    q = build_quote(db, proc, b)
    rnd, newq = negotiate(db, proc, q, 0, ["SUR-CBL-C2C-1M-30W"])
    assert rnd.outcome == "rejected" and rnd.rationale_code == "incompatible_surplus" and newq is None
    assert db.scalar(select(NegotiationRound).where(NegotiationRound.id == rnd.id)).result_quote_id is None


def test_no_feasible_offer_explains_binding_constraints(api):
    p = confirmed_procurement(api, budget_cents=40_000)
    d = run_buyer(api, p["procurement_id"])
    assert d["status"] == "no_feasible_offer"
    assert d["proposal"] is None and d["comparison"]["best_quote_id"] is None
    assert any("Raise the budget to $488.00" in s for s in d["comparison"]["suggestions"])
    assert d["run"]["status"] == "succeeded" and "No feasible offer" in d["run"]["summary"]


def test_late_delivery_is_rejected(api):
    p = confirmed_procurement(api, deadline="2026-11-06")  # B needs 3 days
    d = run_buyer(api, p["procurement_id"])
    b = next(o for o in d["comparison"]["offers"] if o["supplier_id"] == "supplier-b")
    assert "late_delivery" in [r["code"] for r in b["reasons"]]


def test_expired_quote_invalidates_proposal(api):
    p = confirmed_procurement(api)
    d = run_buyer(api, p["procurement_id"])
    api.post("/api/demo/scenarios/expire_quotes")
    r = api.post(f"/api/proposals/{d['proposal']['proposal_id']}/approve", approve_body(d["proposal"]))
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "quote_expired"
    after = api.get(f"/api/procurements/{p['procurement_id']}").json()
    assert after["proposal"]["status"] == "expired" and after["status"] == "draft"


def test_forged_or_stale_digest_is_rejected(api):
    p = confirmed_procurement(api)
    d = run_buyer(api, p["procurement_id"])
    prop = d["proposal"]
    r = api.post(f"/api/proposals/{prop['proposal_id']}/approve", {"version": prop["version"], "digest": "0" * 64})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_proposal"
    r = api.post(f"/api/proposals/{prop['proposal_id']}/approve", {"version": prop["version"] + 1, "digest": prop["digest"]})
    assert r.json()["error"]["code"] == "stale_proposal"
    # A client-supplied total is not part of the contract at all.
    r = api.post(f"/api/proposals/{prop['proposal_id']}/approve", {**approve_body(prop), "total_cents": 1})
    assert r.status_code == 422


def test_double_approval_creates_one_purchase_and_one_reservation(api, db):
    p = confirmed_procurement(api)
    d = run_buyer(api, p["procurement_id"])
    r1 = api.post(f"/api/proposals/{d['proposal']['proposal_id']}/approve", approve_body(d["proposal"]))
    r2 = api.post(f"/api/proposals/{d['proposal']['proposal_id']}/approve", approve_body(d["proposal"]))
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["purchase_id"] == r2.json()["purchase_id"]
    assert len(api.get("/api/purchases").json()["purchases"]) == 1
    item = db.scalar(select(SupplierItem).where(SupplierItem.offered_sku == "CHG-65W-PD", SupplierItem.reserved > 0))
    assert item.reserved == 10


def test_changed_requirements_supersede_proposal(api):
    p = confirmed_procurement(api)
    d = run_buyer(api, p["procurement_id"])
    smaller = {k: v for k, v in BASKET.items() if k != "SCR-TG"}
    confirmed_procurement_id = p["procurement_id"]
    r = api.post(
        f"/api/procurements/{confirmed_procurement_id}/confirm",
        {"lines": [{"sku": k, "qty": v} for k, v in smaller.items()], "budget_cents": 50_000,
         "deadline": "2026-11-07", "coverage_days": 14},
    )
    assert r.json()["proposal"]["status"] == "superseded"
    r = api.post(f"/api/proposals/{d['proposal']['proposal_id']}/approve", approve_body(d["proposal"]))
    assert r.status_code == 409 and r.json()["error"]["code"] == "proposal_not_current"


def test_quotes_are_immutable_rows(api, db):
    p = confirmed_procurement(api)
    run_buyer(api, p["procurement_id"])
    before = {q.id: q.total_cents for q in db.scalars(select(Quote))}
    api.post("/api/demo/scenarios/supplier_a_price_increase")
    db.expire_all()
    assert {q.id: q.total_cents for q in db.scalars(select(Quote))} == before
