import uuid

import httpx


async def test_create_author_trims_and_returns_201(client: httpx.AsyncClient) -> None:
    response = await client.post("/authors", json={"name": " Bob ", "email": " bob@example.com "})
    body = response.json()
    assert response.status_code == 201
    assert response.headers["location"] == f"/authors/{body['id']}"
    assert (body["name"], body["email"]) == ("Bob", "bob@example.com")


async def test_author_validation_collects_all_errors(client: httpx.AsyncClient) -> None:
    response = await client.post("/authors", json={"name": "x" * 51, "email": "not-an-email"})
    assert response.status_code == 400
    reasons = {p["name"]: p["reason"] for p in response.json()["invalidParams"]}
    assert reasons == {
        "name": "name cannot exceed 50 characters",
        "email": "email must be a valid email address",
    }


async def test_duplicate_email_is_409(client: httpx.AsyncClient, author: dict[str, str]) -> None:
    response = await client.post("/authors", json={"name": "Other", "email": author["email"]})
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"

    other = (await client.post("/authors", json={"name": "O", "email": "o@example.com"})).json()
    clash = await client.patch(f"/authors/{other['id']}", json={"email": author["email"]})
    assert clash.status_code == 409


async def test_deleting_an_author_with_messages_is_409_until_they_are_gone(
    client: httpx.AsyncClient, author: dict[str, str]
) -> None:
    message = (
        await client.post(
            "/messages", json={"title": "T", "content": "C", "authorId": author["id"]}
        )
    ).json()

    blocked = await client.delete(f"/authors/{author['id']}")
    assert blocked.status_code == 409  # FK RESTRICT
    assert blocked.json()["code"] == "CONFLICT"

    assert (await client.delete(f"/messages/{message['id']}")).status_code == 204
    assert (await client.delete(f"/authors/{author['id']}")).status_code == 204
    assert (await client.delete(f"/authors/{author['id']}")).status_code == 404


async def test_get_author_and_include_messages(
    client: httpx.AsyncClient, author: dict[str, str]
) -> None:
    await client.post("/messages", json={"title": "T", "content": "C", "authorId": author["id"]})

    plain = (await client.get(f"/authors/{author['id']}")).json()
    assert "messages" not in plain

    detailed = (await client.get(f"/authors/{author['id']}?include=messages")).json()
    assert [m["title"] for m in detailed["messages"]] == ["T"]
    assert "author" not in detailed["messages"][0]  # no author -> messages -> author cycle

    bad = await client.get(f"/authors/{author['id']}?include=everything")
    assert bad.status_code == 400


async def test_update_author(client: httpx.AsyncClient, author: dict[str, str]) -> None:
    response = await client.patch(f"/authors/{author['id']}", json={"name": "Renamed"})
    assert response.status_code == 200
    assert (response.json()["name"], response.json()["email"]) == ("Renamed", author["email"])

    noop = await client.patch(f"/authors/{author['id']}", json={})
    assert noop.status_code == 200

    assert (await client.patch(f"/authors/{uuid.uuid4()}", json={"name": "x"})).status_code == 404
    blank = await client.patch(f"/authors/{author['id']}", json={"name": "  "})
    assert blank.status_code == 400


async def test_list_authors(client: httpx.AsyncClient, author: dict[str, str]) -> None:
    page = (await client.get("/authors")).json()
    assert page["totalCount"] == 1
    assert page["items"][0]["id"] == author["id"]
    assert (await client.get("/authors?limit=0")).status_code == 400
