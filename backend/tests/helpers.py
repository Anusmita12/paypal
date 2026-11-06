from app.jobs import drain

BASKET = {
    "CHG-65W-PD": 10,
    "CBL-C2C-1M": 20,
    "CBL-C2L-1M": 15,
    "EAR-BT-BASIC": 6,
    "CHG-20W-PD": 12,
    "PB-10K": 5,
    "CBL-C2C-2M": 8,
    "SCR-TG": 20,
}
BUSINESS_DATE = "2026-11-04"
DEADLINE = "2026-11-07"  # business date + 3 days: A and C (2 days) and B (3 days) are all on time


def lines(basket=BASKET):
    return [{"sku": k, "qty": v} for k, v in basket.items()]


def confirmed_procurement(api, budget_cents=50_000, deadline=DEADLINE, basket=BASKET, text="Weekend restock under $500"):
    p = api.post("/api/procurements", {"request_text": text}).json()
    r = api.post(
        f"/api/procurements/{p['procurement_id']}/confirm",
        {"lines": lines(basket), "budget_cents": budget_cents, "deadline": deadline, "coverage_days": 14},
    )
    assert r.status_code == 200, r.text
    return r.json()


def run_buyer(api, procurement_id):
    r = api.post(f"/api/procurements/{procurement_id}/run")
    assert r.status_code == 202, r.text
    drain()
    return api.get(f"/api/procurements/{procurement_id}").json()


def approve_body(proposal):
    return {"version": proposal["version"], "digest": proposal["digest"]}
