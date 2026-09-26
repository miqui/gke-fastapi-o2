from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


def _strict_bool(value: object) -> bool:
    # A flag that gates something as sensitive as API docs must not silently pick a side on a
    # typo ("True", "1", "yes"): anything other than exactly "true"/"false" fails startup.
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f'must be exactly "true" or "false", got {value!r}')


StrictBool = Annotated[bool, BeforeValidator(_strict_bool)]


class Settings(BaseSettings):
    """Runtime configuration. Variable names match k8s/configmap.yaml and the postgres-credentials
    Secret, both injected with envFrom."""

    model_config = SettingsConfigDict(extra="ignore")

    port: int = 8080

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "messagedb"
    db_user: str = "message_app"
    db_password: str = "message_app"
    # 6 pods x (5 + 5 overflow) stays well under Postgres' max_connections=100.
    db_pool_size: int = 5
    db_max_overflow: int = 5

    hazelcast_host: str = "localhost"
    hazelcast_port: int = 5701

    otel_metrics_url: str = "http://localhost:4318/v1/metrics"
    otel_traces_url: str = "http://localhost:4318/v1/traces"
    pod_name: str = "local"

    # Off unless enabled: /docs, /redoc and /openapi.json are only served when this is "true"
    # (k8s/configmap.yaml turns it on for this disposable dev cluster).
    api_docs_enabled: StrictBool = False
    # Comma-separated exact origins allowed to call the API from a browser. Unset means no CORS
    # headers at all (same-origin only); curl, k6 and other non-browser clients are unaffected.
    cors_allowed_origins: str = ""

    # Seconds to keep serving after SIGTERM with readiness already DOWN, so the Service/Ingress
    # stop routing to this pod before the listener closes (see app/__main__.py).
    shutdown_drain_seconds: float = 3.0
    max_body_bytes: int = 16 * 1024

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def database_url(self) -> URL:
        # URL.create() escapes the password, so special characters need no manual encoding.
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
