"""RFC 9457 conformance of every error path, and the /problems type documents."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest_asyncio

from app.db import get_session
from app.errors import PROBLEM_TYPES
from app.main import create_app
from app.settings import get_settings

PROBLEM_MEMBERS = {"type", "title", "status", "detail", "instance", "code"}


def assert_problem(
    response: httpx.Response, status: int, code: str, type_: str, instance: str
) -> dict[str, Any]:
    body = response.json()
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert PROBLEM_MEMBERS <= body.keys()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"] == type_
    assert body["instance"] == instance
    assert isinstance(body["title"], str) and body["title"]
    assert isinstance(body["detail"], str) and body["detail"]
    return body


async def test_not_found_names_the_resource_as_instance(client: httpx.AsyncClient) -> None:
    path = f"/messages/{uuid.uuid4()}"
    body = assert_problem(
        await client.get(path), 404, "NOT_FOUND", "/problems/not-found", instance=path
    )
    assert body["title"] == "Not Found"
    assert "invalidParams" not in body


async def test_unknown_route_is_a_not_found_problem(client: httpx.AsyncClient) -> None:
    assert_problem(
        await client.get("/nope"), 404, "NOT_FOUND", "/problems/not-found", instance="/nope"
    )


async def test_unsupported_method_is_about_blank(client: httpx.AsyncClient) -> None:
    # A 405 means nothing beyond its status code, so RFC 9457 says `about:blank`.
    response = await client.put("/messages")
    body = assert_problem(response, 405, "HTTP_405", "about:blank", instance="/messages")
    assert body["title"] == "Method Not Allowed"
    assert "GET" in response.headers["allow"]  # headers from the framework's exception survive


async def test_stale_version_is_a_conflict_problem(
    client: httpx.AsyncClient, author: dict[str, str]
) -> None:
    created = (
        await client.post(
            "/messages", json={"title": "T", "content": "C", "authorId": author["id"]}
        )
    ).json()
    path = f"/messages/{created['id']}"
    await client.patch(path, json={"content": "first", "version": 0})

    stale = await client.patch(path, json={"content": "stale", "version": 0})
    assert_problem(stale, 409, "CONFLICT", "/problems/conflict", instance=path)


async def test_oversized_body_is_413_with_its_own_type(client: httpx.AsyncClient) -> None:
    too_big = "x" * (get_settings().max_body_bytes + 1)
    response = await client.post(
        "/messages", content=too_big, headers={"content-type": "application/json"}
    )
    body = assert_problem(
        response, 413, "BAD_USER_INPUT", "/problems/payload-too-large", instance="/messages"
    )
    assert body["title"] == "Content Too Large"


async def test_instance_is_percent_encoded(client: httpx.AsyncClient) -> None:
    response = await client.get("/no such/ünï")
    assert response.json()["instance"] == "/no%20such/%C3%BCn%C3%AF"


@pytest_asyncio.fixture
async def broken_client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(telemetry=False)

    async def boom() -> None:
        raise RuntimeError("secret connection string postgres://user:hunter2@db")

    app.dependency_overrides[get_session] = boom
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_unhandled_exception_is_a_generic_500_problem(
    broken_client: httpx.AsyncClient,
) -> None:
    response = await broken_client.get("/messages")
    body = assert_problem(
        response,
        500,
        "INTERNAL_SERVER_ERROR",
        "/problems/internal-server-error",
        instance="/messages",
    )
    assert body["detail"] == "An unexpected error occurred."
    assert "hunter2" not in response.text and "RuntimeError" not in response.text


async def test_problem_types_are_dereferenceable(client: httpx.AsyncClient) -> None:
    listing = await client.get("/problems")
    assert listing.status_code == 200
    assert {p["type"] for p in listing.json()} == {t.uri for t in PROBLEM_TYPES.values()}

    for problem in PROBLEM_TYPES.values():
        response = await client.get(problem.uri)
        assert response.status_code == 200
        doc = response.json()
        assert (doc["type"], doc["status"], doc["code"]) == (
            problem.uri,
            problem.status,
            problem.code,
        )
        assert doc["description"]

    assert_problem(
        await client.get("/problems/unknown"),
        404,
        "NOT_FOUND",
        "/problems/not-found",
        instance="/problems/unknown",
    )


async def test_every_type_a_response_can_carry_is_registered(client: httpx.AsyncClient) -> None:
    for path, slug in (
        ("/messages/not-a-uuid", "bad-user-input"),
        (f"/authors/{uuid.uuid4()}", "not-found"),
    ):
        body = (await client.get(path)).json()
        assert body["type"] == PROBLEM_TYPES[slug].uri


def test_openapi_documents_problem_json_errors_not_422() -> None:
    # Served at /openapi.json only when API_DOCS_ENABLED; the generator is what matters here.
    spec = create_app(telemetry=False).openapi()
    operation = spec["paths"]["/messages/{id}"]["patch"]
    responses = operation["responses"]

    assert "422" not in responses
    assert {"400", "404", "409", "413", "500"} <= responses.keys()
    for status in ("400", "404", "409", "413", "500"):
        content = responses[status]["content"]
        assert list(content) == ["application/problem+json"]
        assert content["application/problem+json"]["schema"] == {
            "$ref": "#/components/schemas/Problem"
        }
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    assert set(spec["components"]["schemas"]["Problem"]["required"]) == PROBLEM_MEMBERS
