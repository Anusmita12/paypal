import threading
from datetime import timedelta

from sqlalchemy import select

from app.approvals import expire_reservations
from app.db import get_sessionmaker
from app.jobs import RetryableJobError, drain, enqueue, handler
from app.models import Job, Purchase, Reservation, SupplierItem
from app.suppliers import utcnow
from tests.helpers import approve_body, confirmed_procurement, run_buyer


def _proposal(api):
    d = run_buyer(api, confirmed_procurement(api)["procurement_id"])
    return d["proposal"]


def test_concurrent_approvals_cannot_overdraw_supplier_stock(api, db):
    p1, p2 = _proposal(api), _proposal(api)
    assert p1["snapshot"]["supplier"]["id"] == p2["snapshot"]["supplier"]["id"] == "supplier-a"
    # Leave Apex with stock for exactly one basket of 65W chargers.
    item = db.scalar(select(SupplierItem).where(SupplierItem.offered_sku == "CHG-65W-PD", SupplierItem.unit_cents == 1450))
    item.stock = 15
    db.commit()

    results = []
    barrier = threading.Barrier(2)

    def approve(p):
        barrier.wait()
        r = api.post(f"/api/proposals/{p['proposal_id']}/approve", approve_body(p))
        results.append((r.status_code, r.json().get("error", {}).get("code")))

    threads = [threading.Thread(target=approve, args=(p,)) for p in (p1, p2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [(200, None), (409, "insufficient_stock")]
    db.expire_all()
    assert db.get(SupplierItem, item.id).reserved == 10


def test_expired_hold_is_released_but_pinned_capture_is_not(api, db):
    p1, p2 = _proposal(api), _proposal(api)
    b1 = api.post(f"/api/proposals/{p1['proposal_id']}/approve", approve_body(p1)).json()
    b2 = api.post(f"/api/proposals/{p2['proposal_id']}/approve", approve_body(p2)).json()
    r2 = db.scalar(select(Reservation).where(Reservation.purchase_id == b2["purchase_id"]))
    r2.status = "capture_in_progress"  # capture started; outcome unknown
    db.get(Purchase, b2["purchase_id"]).status = "unknown"
    db.commit()

    released = expire_reservations(db, now=utcnow() + timedelta(hours=1))
    db.commit()
    assert released == 1
    db.expire_all()
    assert db.scalar(select(Reservation).where(Reservation.purchase_id == b1["purchase_id"])).status == "released"
    assert db.get(Purchase, b1["purchase_id"]).status == "cancelled"
    assert db.get(Reservation, r2.id).status == "capture_in_progress"
    item = db.scalar(select(SupplierItem).where(SupplierItem.offered_sku == "CHG-65W-PD", SupplierItem.unit_cents == 1450))
    assert item.reserved == 10  # only the pinned reservation still holds stock


CALLS: list[str] = []


@handler("test_flaky")
def _flaky(db, payload):
    CALLS.append(payload["n"])
    if len(CALLS) < 3:
        raise RetryableJobError("transient")


@handler("test_broken")
def _broken(db, payload):
    raise ValueError("permanent")


def test_jobs_retry_with_backoff_then_succeed(db):
    CALLS.clear()
    enqueue(db, "test_flaky", {"n": 1}, dedupe_key="flaky:1")
    enqueue(db, "test_flaky", {"n": 1}, dedupe_key="flaky:1")  # deduplicated while open
    db.commit()
    assert db.scalar(select(Job).where(Job.kind == "test_flaky")) is not None
    assert len(db.scalars(select(Job)).all()) == 1
    far = utcnow() + timedelta(minutes=10)
    for _ in range(3):
        drain(now=far)
    db.expire_all()
    job = db.scalar(select(Job))
    assert job.status == "processed" and job.attempts == 3 and len(CALLS) == 3 and job.dedupe_key is None


def test_permanent_job_failure_is_recorded(db):
    enqueue(db, "test_broken", {})
    db.commit()
    drain()
    job = db.scalar(select(Job))
    db.refresh(job)
    assert job.status == "failed" and "permanent" in job.last_error


def test_crashed_job_lease_is_reclaimed(db):
    enqueue(db, "test_flaky", {"n": 9})
    db.commit()
    job = db.scalar(select(Job))
    job.status, job.lease_until, job.attempts = "processing", utcnow() - timedelta(seconds=1), 1
    db.commit()
    CALLS.clear()
    CALLS.extend(["a", "b"])  # next call succeeds
    with get_sessionmaker()() as s:
        drain()
        assert s.get(Job, job.id).status == "processed"
