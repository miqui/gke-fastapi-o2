from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.db import session_factory
from app.models import Author, Message

SYSTEM_EMAIL = "system@message-service.local"
# Distinct from the migration lock key in migrations/env.py.
SEED_LOCK_KEY = 7_265_490_001


async def seed_initial_message() -> None:
    """Pre-populate a welcome message (and its system author) on a fresh deployment.

    Idempotent, and safe with all replicas starting at once: a transaction-scoped advisory lock
    serialises the check-then-insert, so exactly one welcome message is created.
    """
    async with session_factory() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SEED_LOCK_KEY})

        if (await session.execute(select(func.count()).select_from(Message))).scalar_one() > 0:
            return

        await session.execute(
            insert(Author)
            .values(name="system", email=SYSTEM_EMAIL)
            .on_conflict_do_nothing(index_elements=[Author.email])
        )
        author_id = (
            await session.execute(select(Author.id).where(Author.email == SYSTEM_EMAIL))
        ).scalar_one()
        session.add(
            Message(
                title="Welcome to the Kubernetes REST API",
                content="A sample message backed by FastAPI and PostgreSQL on a kind cluster.",
                author_id=author_id,
            )
        )
