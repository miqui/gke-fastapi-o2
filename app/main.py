import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.cache import cache
from app.db import engine
from app.errors import install_openapi, problem_responses, register_error_handlers
from app.middleware import BodyLimitMiddleware
from app.routers import authors, health, messages, problems
from app.seed import seed_initial_message
from app.settings import get_settings
from app.state import state
from app.telemetry import RequestMetricsMiddleware, setup_metrics, shutdown_metrics
from app.tracing import setup_tracing, shutdown_tracing

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    setup_metrics(engine)

    # The cache is not optional: an unreachable Hazelcast member fails startup (the pod restarts)
    # rather than silently serving without it.
    await cache.connect(settings.hazelcast_host, settings.hazelcast_port)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    await seed_initial_message()

    state.ready = True
    log.info(
        "message-service started",
        extra={
            "api_docs_enabled": settings.api_docs_enabled,
            "cors": settings.cors_origins or "same-origin-only",
        },
    )
    yield

    # Shutdown. Readiness is already DOWN and the listener closed with in-flight requests drained
    # by the time uvicorn runs this (see app/__main__.py); what is left is releasing resources:
    # cache client, DB pool, then flush metrics and traces.
    state.ready = False
    log.info("shutting down")
    await cache.shutdown()
    await engine.dispose()
    await shutdown_metrics()
    await shutdown_tracing()


def create_app(*, telemetry: bool = True) -> FastAPI:
    settings = get_settings()
    docs = settings.api_docs_enabled
    app = FastAPI(
        title="Message Service",
        version="0.1.0",
        description="REST API for messages and authors (see API-DESIGN.md).",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        responses=problem_responses(500),
    )
    register_error_handlers(app)
    install_openapi(app)
    app.include_router(health.router)
    app.include_router(messages.router)
    app.include_router(authors.router)
    app.include_router(problems.router)

    # add_middleware() wraps: the last one added is outermost. Order (outer -> inner): OTel span,
    # request metrics, CORS, body limit, app.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Content-Type"],
        )
    app.add_middleware(RequestMetricsMiddleware)
    if telemetry:
        setup_tracing(app, engine)
    return app
