"""Integration tests run against a real Postgres (never the dev `messagedb`): by default
`messagedb_test` on localhost, overridable with the same DB_* variables the service uses, e.g.

    docker run -d --name msg-pg -e POSTGRES_USER=message_app -e POSTGRES_PASSWORD=message_app \\
        -e POSTGRES_DB=messagedb_test -p 5432:5432 postgres:16-alpine
    uv run pytest

The schema comes from the real Alembic migration, so the migration is tested too. Hazelcast is
replaced by an in-memory fake.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator

os.environ.setdefault("DB_NAME", "messagedb_test")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.cache import get_cache  # noqa: E402
from app.db import engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.state import state  # noqa: E402


class FakeCache:
    """In-memory stand-in for Hazelcast that records every call."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    async def get(self, message_id: str) -> str | None:
        self.calls.append(("get", message_id))
        return self.data.get(message_id)

    async def set(self, message_id: str, value: str) -> None:
        self.calls.append(("set", message_id))
        self.data[message_id] = value

    async def evict(self, message_id: str) -> None:
        self.calls.append(("evict", message_id))
        self.data.pop(message_id, None)


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], capture_output=True, text=True
    )
    if result.returncode != 0:
        pytest.exit(
            f"alembic upgrade head failed - is Postgres up?\n{result.stdout}{result.stderr}"
        )
    yield


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(migrated_database: None) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE messages, authors"))
    yield


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


@pytest_asyncio.fixture
async def client(cache: FakeCache) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(telemetry=False)
    app.dependency_overrides[get_cache] = lambda: cache
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    state.ready = False
    yield
    state.ready = False


@pytest_asyncio.fixture
async def author(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post("/authors", json={"name": "Ann", "email": "ann@example.com"})
    assert response.status_code == 201
    return response.json()
