"""Async Alembic environment.

Migrations run at container start (see the Dockerfile ENTRYPOINT) on every replica at once, so
the whole run holds a session-level Postgres advisory lock: replicas queue on it, the first one
migrates, the rest find nothing to do. (Alembic takes no lock of its own.) The lock lives on
its own AUTOCOMMIT connection so it cannot interfere with the migration transaction.
"""

import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.logging import setup_logging
from app.models import Base
from app.settings import get_settings

# Arbitrary fixed bigint; must differ from app/seed.py's SEED_LOCK_KEY.
MIGRATION_LOCK_KEY = 7_265_490_000

setup_logging()
target_metadata = Base.metadata


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as lock_conn, engine.connect() as conn:
        lock_conn = await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
        await lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        try:
            await conn.run_sync(_run_migrations)
        finally:
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline (--sql) migrations are not supported: they would skip the lock.")
asyncio.run(run_migrations_online())
