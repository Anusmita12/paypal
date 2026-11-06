"""Operational commands: python -m app.cli {check-config|migrate|seed|run-jobs|expire-reservations|reconcile-pending}."""

import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import get_settings
from app.db import get_sessionmaker

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def check_config() -> int:
    settings = get_settings()
    problems = settings.problems()
    print(f"APP_ENV={settings.app_env} AI_MODE={settings.ai_mode} PAYMENT_MODE={settings.payment_mode} "
          f"CHANNEL3_MODE={settings.channel3_mode} ENABLE_SCENARIOS={settings.enable_scenarios}")
    if problems:
        print("Configuration errors:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("Configuration OK.")
    return 0


def migrate() -> int:
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    return 0


def seed() -> int:
    from app.seed import seed_global

    with get_sessionmaker()() as db:
        n = seed_global(db)
        db.commit()
    print(f"Seeded {n} paid tools (idempotent). Demo retailers are created per session.")
    return 0


def run_jobs() -> int:
    import app.agent
    import app.disputes
    import app.payments
    import app.webhooks  # noqa: F401
    from app.jobs import drain

    print(f"Processed {drain()} job(s).")
    return 0


def expire_reservations() -> int:
    from app.approvals import expire_reservations as expire

    with get_sessionmaker()() as db:
        n = expire(db)
        db.commit()
    print(f"Released {n} expired reservation(s).")
    return 0


def reconcile_pending() -> int:
    """Check every unresolved purchase that already talked to PayPal; reuses persisted request IDs."""
    from sqlalchemy import select

    from app.models import Purchase
    from app.payments import ProviderTimeout, reconcile

    sm = get_sessionmaker()
    with sm() as db:
        ids = list(db.scalars(select(Purchase.id).where(
            Purchase.status.in_(["unknown", "capture_pending", "awaiting_paypal_approval", "creating"]))))
    for pid in ids:
        with sm() as db:
            try:
                p = reconcile(db, pid, source="reconciliation")
                print(f"{pid}: {p.status}")
            except ProviderTimeout as exc:
                print(f"{pid}: still unknown ({exc})")
    print(f"Checked {len(ids)} purchase(s).")
    return 0


COMMANDS = {
    "check-config": check_config,
    "migrate": migrate,
    "seed": seed,
    "run-jobs": run_jobs,
    "expire-reservations": expire_reservations,
    "reconcile-pending": reconcile_pending,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=sorted(COMMANDS))
    sys.exit(COMMANDS[parser.parse_args().command]())


if __name__ == "__main__":
    main()
