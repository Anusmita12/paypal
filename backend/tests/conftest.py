import os

# Tests always use the isolated test database and offline adapters, whatever .env says.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://tradepilot:local-dev-only@localhost:5432/tradepilot_test"
)
os.environ.update(
    APP_ENV="test",
    AI_MODE="fixture",
    PAYMENT_MODE="fixture",
    CHANNEL3_MODE="fixture",
    ENABLE_SCENARIOS="true",
    DEMO_BUSINESS_DATE="2026-11-04",  # a Wednesday
    RUN_JOBS_IN_PROCESS="false",
    PUBLIC_BASE_URL="http://testserver",
    ALLOWED_ORIGINS="http://localhost:5173",
    MAX_SESSIONS_PER_IP_PER_HOUR="1000",
)

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import make_url, text

from app.cli import ALEMBIC_INI
from app.db import get_engine, get_sessionmaker
from app.main import create_app

KEEP = {"alembic_version", "paid_tools"}


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    url = make_url(os.environ["DATABASE_URL"])
    assert url.database and url.database.endswith("_test"), "refusing to run tests against a non-test database"
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    yield


@pytest.fixture(autouse=True)
def clean_db(migrated_db):
    engine = get_engine()
    with engine.begin() as conn:
        tables = [r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))]
        doomed = [t for t in tables if t not in KEEP]
        if doomed:
            conn.execute(text(f"TRUNCATE {', '.join(doomed)} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def app():
    return create_app()


class Api:
    """A browser-like client: one demo session cookie plus the CSRF header."""

    def __init__(self, app):
        self.client = TestClient(app)
        self.client.__enter__()
        r = self.client.post("/api/demo/session")
        assert r.status_code == 200, r.text
        self.session = r.json()
        self.csrf = self.session["csrf_token"]

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, json=None, **kw):
        headers = {"x-csrf-token": self.csrf, **kw.pop("headers", {})}
        return self.client.post(path, json=json, headers=headers, **kw)

    def close(self):
        self.client.__exit__(None, None, None)


@pytest.fixture
def api(app):
    a = Api(app)
    yield a
    a.close()


@pytest.fixture
def other_api(app):
    a = Api(app)
    yield a
    a.close()


@pytest.fixture
def db():
    with get_sessionmaker()() as s:
        yield s
