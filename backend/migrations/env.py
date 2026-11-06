from alembic import context
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401 - registers tables on Base.metadata
from app.config import get_settings
from app.db import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = context.config.attributes.get("database_url") or get_settings().database_url
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
