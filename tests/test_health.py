import httpx
import pytest

from app.settings import Settings
from app.state import state


async def test_liveness_is_always_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/liveness")
    assert response.status_code == 200
    assert response.json() == {"status": "UP"}


async def test_readiness_is_503_before_startup_completes(client: httpx.AsyncClient) -> None:
    # state.ready only flips after the DB, the cache and the seed are up (see app/main.py).
    response = await client.get("/health/readiness")
    assert response.status_code == 503
    assert response.json() == {"status": "DOWN"}


async def test_readiness_is_up_once_ready_and_down_again_on_shutdown(
    client: httpx.AsyncClient,
) -> None:
    state.ready = True
    assert (await client.get("/health/readiness")).json() == {"status": "UP"}
    state.ready = False
    assert (await client.get("/health/readiness")).status_code == 503


async def test_api_docs_are_off_unless_enabled(client: httpx.AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


@pytest.mark.parametrize("value", ["True", "1", "yes", "", "enabled"])
def test_api_docs_flag_must_be_exactly_true_or_false(value: str) -> None:
    with pytest.raises(ValueError, match="true"):
        Settings(api_docs_enabled=value)  # type: ignore[arg-type]


def test_api_docs_flag_accepts_true_and_false() -> None:
    assert Settings(api_docs_enabled="true").api_docs_enabled is True  # type: ignore[arg-type]
    assert Settings(api_docs_enabled="false").api_docs_enabled is False  # type: ignore[arg-type]


def test_database_url_escapes_the_password() -> None:
    url = Settings(db_password="p@ss/w:rd?").database_url.render_as_string(hide_password=False)
    assert "p%40ss%2Fw%3Ard%3F" in url
