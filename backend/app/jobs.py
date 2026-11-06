"""Persisted jobs with leases. One in-process poller; the documented MVP ceiling is one app instance."""

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db import get_sessionmaker
from app.models import Job
from app.suppliers import utcnow

log = logging.getLogger("tradepilot.jobs")
LEASE_SECONDS = 180

Handler = Callable[[Session, dict], None]
HANDLERS: dict[str, Handler] = {}


class RetryableJobError(Exception):
    pass


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return register


def enqueue(db: Session, kind: str, payload: dict, dedupe_key: str | None = None, run_after: datetime | None = None) -> None:
    """Insert a job. An open job with the same dedupe_key makes this a no-op."""
    db.execute(
        insert(Job)
        .values(kind=kind, payload=payload, dedupe_key=dedupe_key, run_after=run_after or utcnow())
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )


def claim(db: Session, now: datetime | None = None) -> Job | None:
    now = now or utcnow()
    job = db.scalar(
        select(Job)
        .where(
            or_(
                (Job.status == "pending") & (Job.run_after <= now),
                (Job.status == "processing") & (Job.lease_until < now),  # crashed worker: lease expired
            )
        )
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job:
        job.status = "processing"
        job.attempts += 1
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        db.commit()
    return job


def run_one(now: datetime | None = None) -> bool:
    """Claim and execute one job. Returns False when nothing was due."""
    sm = get_sessionmaker()
    with sm() as db:
        job = claim(db, now)
        if not job:
            return False
        job_id, kind, payload = job.id, job.kind, dict(job.payload)
    with sm() as db:
        try:
            fn = HANDLERS.get(kind)
            if fn is None:
                raise RuntimeError(f"no handler for job kind {kind}")
            fn(db, payload)
            db.commit()
            outcome, error = "processed", None
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
            db.rollback()
            outcome, error = ("retry" if isinstance(exc, RetryableJobError) else "failed"), f"{type(exc).__name__}: {exc}"
            log.warning("job %s (%s) %s: %s", job_id, kind, outcome, error)
    with sm() as db:
        job = db.get(Job, job_id)
        job.lease_until = None
        job.last_error = error
        if outcome == "retry" and job.attempts < job.max_attempts:
            delay = min(2 ** job.attempts, 60) + random.uniform(0, 1)
            job.status, job.run_after = "pending", utcnow() + timedelta(seconds=delay)
        else:
            job.status = "processed" if outcome == "processed" else "failed"
            job.dedupe_key = None  # closed jobs no longer block a new job with the same key
        db.commit()
    return True


def drain(max_jobs: int = 100, now: datetime | None = None) -> int:
    """Run due jobs synchronously (tests and CLI)."""
    n = 0
    while n < max_jobs and run_one(now):
        n += 1
    return n


async def poller(stop: asyncio.Event, interval: float) -> None:
    log.info("job poller started")
    while not stop.is_set():
        try:
            worked = await asyncio.to_thread(run_one)
        except Exception:
            log.exception("job poller iteration failed")
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
