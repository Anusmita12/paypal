from tests.helpers import DEADLINE, approve_body, confirmed_procurement, lines, run_buyer


def test_mutations_require_csrf_and_same_origin(api):
    r = api.client.post("/api/procurements", json={"request_text": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = api.post("/api/procurements", {"request_text": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"
    r = api.post("/api/procurements", {"request_text": "x"}, headers={"origin": "http://localhost:5173"})
    assert r.status_code == 200


def test_session_cookie_is_httponly(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/demo/session")
        cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("tp_session="))
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_cross_session_access_is_denied(api, other_api):
    p = confirmed_procurement(api)
    d = run_buyer(api, p["procurement_id"])
    assert other_api.get(f"/api/procurements/{p['procurement_id']}").status_code == 404
    assert other_api.post(f"/api/procurements/{p['procurement_id']}/run").status_code == 404
    r = other_api.post(f"/api/proposals/{d['proposal']['proposal_id']}/approve", approve_body(d["proposal"]))
    assert r.status_code == 404
    purchase = api.post(f"/api/proposals/{d['proposal']['proposal_id']}/approve", approve_body(d["proposal"])).json()
    assert other_api.get(f"/api/purchases/{purchase['purchase_id']}").status_code == 404
    assert other_api.get("/api/purchases").json()["purchases"] == []


def test_ambiguous_text_never_binds_requirements(api):
    p = api.post("/api/procurements", {"request_text": "restock everything, spend whatever, asap"}).json()
    assert p["status"] == "needs_input" and p["requirements"] is None
    assert p["proposed"]["budget_cents"] is None and p["proposed"]["deadline"] is None
    r = api.post(f"/api/procurements/{p['procurement_id']}/run")
    assert r.status_code == 409 and r.json()["error"]["code"] == "requirements_not_confirmed"


def test_text_hints_are_suggestions_only(api):
    p = api.post("/api/procurements", {"request_text": "Keep it below $500 and deliver by Friday"}).json()
    assert p["proposed"]["budget_cents"] == 50_000
    assert p["proposed"]["deadline"] == "2026-11-06"  # next Friday after Wednesday 2026-11-04
    assert p["requirements"] is None


def test_non_usd_request_gets_clear_message(api):
    p = api.post("/api/procurements", {"request_text": "Budget is 40000 rupees"}).json()
    assert any("Only USD" in n for n in p["proposed"]["notes"])
    r = api.post(
        f"/api/procurements/{p['procurement_id']}/confirm",
        {"lines": lines(), "budget_cents": 50_000, "deadline": DEADLINE, "currency": "INR"},
    )
    assert r.json()["error"]["code"] == "unsupported_currency"


def test_requirement_validation(api):
    pid = api.post("/api/procurements", {"request_text": ""}).json()["procurement_id"]

    def confirm(**kw):
        body = {"lines": lines(), "budget_cents": 50_000, "deadline": DEADLINE, "coverage_days": 14, **kw}
        return api.post(f"/api/procurements/{pid}/confirm", body).json().get("error", {}).get("code")

    assert confirm(lines=[{"sku": "NOPE", "qty": 1}]) == "unknown_sku"
    assert confirm(lines=[{"sku": "SCR-TG", "qty": 0}]) == "invalid_quantity"
    assert confirm(lines=[{"sku": "SCR-TG", "qty": 501}]) == "invalid_quantity"
    assert confirm(lines=[{"sku": "SCR-TG", "qty": 1}, {"sku": "SCR-TG", "qty": 2}]) == "duplicate_sku"
    assert confirm(budget_cents=5) == "invalid_budget"
    assert confirm(deadline="2026-11-04") == "invalid_deadline"
    assert confirm(deadline="2027-03-01") == "invalid_deadline"
    assert confirm() is None


def test_scenarios_cannot_touch_payments(api):
    r = api.post("/api/demo/scenarios/mark_paid")
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_scenario"


def test_ready_reports_migration(api):
    r = api.get("/api/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_relative_deadline_hint_is_a_suggestion_only(api):
    p = api.post("/api/procurements", {"request_text": "Restock under $500, delivered within 3 days"}).json()
    assert p["status"] == "needs_input" and p["requirements"] is None
    assert p["proposed"]["deadline"] == "2026-11-07" and p["proposed"]["budget_cents"] == 50_000


def test_analytics_sources_are_session_scoped(api, other_api):
    from tests.helpers import confirmed_procurement, run_buyer

    run_buyer(api, confirmed_procurement(api)["procurement_id"])
    mine = api.get("/api/analytics").json()["sources"]
    assert len(mine["offers"]) >= 3 and len(mine["tool_usage"]) == 8 and len(mine["inventory"]) == 20
    theirs = other_api.get("/api/analytics").json()["sources"]
    assert theirs["offers"] == [] and theirs["tool_usage"] == []
