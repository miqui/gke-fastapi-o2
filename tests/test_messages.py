import asyncio
import uuid

import httpx
from sqlalchemy import func, select

from app.db import session_factory
from app.models import Message
from app.seed import seed_initial_message
from tests.conftest import FakeCache


async def _create(client: httpx.AsyncClient, author_id: str, title: str = "T") -> dict[str, object]:
    response = await client.post(
        "/messages", json={"title": title, "content": "C", "authorId": author_id}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_returns_201_location_and_embedded_author(
    client: httpx.AsyncClient, author: dict[str, str]
) -> None:
    response = await client.post(
        "/messages", json={"title": "  Hi  ", "content": "Body", "authorId": author["id"]}
    )
    body = response.json()
    assert response.status_code == 201
    assert response.headers["location"] == f"/messages/{body['id']}"
    assert body["title"] == "Hi"  # trimmed
    assert body["version"] == 0
    assert body["author"]["email"] == "ann@example.com"
    assert "createdAt" in body and "created_at" not in body


async def test_validation_collects_all_errors_as_problem_json(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/messages", json={"title": "", "content": "x" * 1001, "authorId": "not-a-uuid"}
    )
    body = response.json()
    assert response.status_code == 400  # FastAPI's default 422 is overridden
    assert response.headers["content-type"] == "application/problem+json"
    assert body["type"] == "/problems/bad-user-input"
    assert body["title"] == "Bad Request"
    assert body["status"] == 400
    assert body["instance"] == "/messages"
    assert body["code"] == "BAD_USER_INPUT"
    assert {p["name"] for p in body["invalidParams"]} == {"title", "content", "authorId"}
    reasons = {p["name"]: p["reason"] for p in body["invalidParams"]}
    assert reasons["title"] == "title is required and cannot be blank"
    assert reasons["content"] == "content cannot exceed 1000 characters"
    assert "stack" not in response.text.lower()


async def test_missing_fields_and_malformed_json_are_400(client: httpx.AsyncClient) -> None:
    missing = await client.post("/messages", json={})
    assert missing.status_code == 400
    assert {p["name"] for p in missing.json()["invalidParams"]} == {"title", "content", "authorId"}

    malformed = await client.post(
        "/messages", content=b"{bad", headers={"content-type": "application/json"}
    )
    assert malformed.status_code == 400
    assert malformed.json()["code"] == "BAD_USER_INPUT"


async def test_create_with_unknown_author_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/messages", json={"title": "T", "content": "C", "authorId": str(uuid.uuid4())}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_get_is_cache_aside(
    client: httpx.AsyncClient, author: dict[str, str], cache: FakeCache
) -> None:
    created = await _create(client, author["id"])
    mid = str(created["id"])

    first = await client.get(f"/messages/{mid}")  # miss: loads from the DB, populates the cache
    assert first.status_code == 200
    assert cache.calls == [("get", mid), ("set", mid)]

    cache.calls.clear()
    second = await client.get(f"/messages/{mid}")  # hit: served from the cache
    assert second.json() == first.json()
    assert cache.calls == [("get", mid)]


async def test_get_unknown_or_malformed_id(client: httpx.AsyncClient) -> None:
    missing = await client.get(f"/messages/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"

    bad = await client.get("/messages/not-a-uuid")
    assert bad.status_code == 400
    assert bad.json()["invalidParams"] == [{"name": "id", "reason": "id must be a valid UUID"}]


async def test_update_bumps_version_and_evicts_cache(
    client: httpx.AsyncClient, author: dict[str, str], cache: FakeCache
) -> None:
    mid = str((await _create(client, author["id"], "Old"))["id"])
    await client.get(f"/messages/{mid}")  # populate the cache
    assert mid in cache.data

    response = await client.patch(
        f"/messages/{mid}", json={"title": "New", "content": "New body", "version": 0}
    )
    body = response.json()
    assert response.status_code == 200
    assert (body["title"], body["content"], body["version"]) == ("New", "New body", 1)
    assert ("evict", mid) in cache.calls
    assert mid not in cache.data

    # A null/absent title leaves it unchanged.
    again = await client.patch(f"/messages/{mid}", json={"content": "Again", "version": 1})
    assert (again.json()["title"], again.json()["version"]) == ("New", 2)


async def test_stale_version_is_409_and_does_not_overwrite(
    client: httpx.AsyncClient, author: dict[str, str]
) -> None:
    mid = str((await _create(client, author["id"]))["id"])
    assert (
        await client.patch(f"/messages/{mid}", json={"content": "first", "version": 0})
    ).status_code == 200

    stale = await client.patch(f"/messages/{mid}", json={"content": "stale write", "version": 0})
    assert stale.status_code == 409
    assert stale.json()["code"] == "CONFLICT"

    current = (await client.get(f"/messages/{mid}")).json()
    assert (current["content"], current["version"]) == ("first", 1)


async def test_stale_version_evicts_a_possibly_stale_cache_entry(
    client: httpx.AsyncClient, author: dict[str, str], cache: FakeCache
) -> None:
    mid = str((await _create(client, author["id"]))["id"])
    await client.patch(f"/messages/{mid}", json={"content": "v1", "version": 0})
    cache.data[mid] = '{"stale": true}'  # a slow reader repopulated the cache with old data

    response = await client.patch(f"/messages/{mid}", json={"content": "x", "version": 0})
    assert response.status_code == 409
    assert mid not in cache.data  # healed: the next read reloads from the DB


async def test_update_unknown_message_is_404_not_409(client: httpx.AsyncClient) -> None:
    response = await client.patch(f"/messages/{uuid.uuid4()}", json={"content": "x", "version": 0})
    assert response.status_code == 404


async def test_concurrent_updates_never_lose_a_write(
    client: httpx.AsyncClient, author: dict[str, str]
) -> None:
    """The k6-transaction-isolation scenario in miniature: every 200 is a distinct +1."""
    mid = str((await _create(client, author["id"]))["id"])
    responses = await asyncio.gather(
        *(
            client.patch(f"/messages/{mid}", json={"content": f"w{i}", "version": 0})
            for i in range(10)
        )
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] + [409] * 9  # all read version 0: exactly one wins
    assert (await client.get(f"/messages/{mid}")).json()["version"] == 1


async def test_update_validation(client: httpx.AsyncClient, author: dict[str, str]) -> None:
    mid = str((await _create(client, author["id"]))["id"])
    response = await client.patch(
        f"/messages/{mid}", json={"title": "  ", "content": "", "version": -1}
    )
    assert response.status_code == 400
    assert {p["name"] for p in response.json()["invalidParams"]} == {"title", "content", "version"}


async def test_delete_evicts_cache_then_404s(
    client: httpx.AsyncClient, author: dict[str, str], cache: FakeCache
) -> None:
    mid = str((await _create(client, author["id"]))["id"])
    await client.get(f"/messages/{mid}")

    assert (await client.delete(f"/messages/{mid}")).status_code == 204
    assert ("evict", mid) in cache.calls
    assert mid not in cache.data
    assert (await client.get(f"/messages/{mid}")).status_code == 404
    assert (await client.delete(f"/messages/{mid}")).status_code == 404


async def test_list_pagination_order_and_bounds(
    client: httpx.AsyncClient, author: dict[str, str]
) -> None:
    ids = [str((await _create(client, author["id"], f"m{i}"))["id"]) for i in range(5)]

    page = (await client.get("/messages?limit=2&offset=1")).json()
    assert page["totalCount"] == 5
    assert [m["id"] for m in page["items"]] == ids[::-1][1:3]  # newest first

    assert len((await client.get("/messages")).json()["items"]) == 5  # default limit 50

    for query in ("limit=0", "limit=201", "offset=-1", "limit=abc"):
        bad = await client.get(f"/messages?{query}")
        assert bad.status_code == 400, query  # rejected, never silently clamped
        assert bad.json()["code"] == "BAD_USER_INPUT"


async def test_oversized_body_is_413(client: httpx.AsyncClient) -> None:
    response = await client.post("/messages", json={"title": "a" * 20_000})
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"


async def test_unhandled_error_is_a_generic_500_without_details(
    client: httpx.AsyncClient,
) -> None:
    app = client._transport.app  # type: ignore[attr-defined]

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals")

    response = await client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_SERVER_ERROR"
    assert "secret" not in response.text and "Traceback" not in response.text


async def test_seed_is_idempotent_even_when_replicas_start_together() -> None:
    await asyncio.gather(*(seed_initial_message() for _ in range(3)))
    await seed_initial_message()
    async with session_factory() as session:
        count = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
    assert count == 1
