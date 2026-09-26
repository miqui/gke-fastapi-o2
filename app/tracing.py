"""Traces: OTLP/HTTP push to the collector (which forwards to OpenObserve).

Sampling comes from the SDK's standard OTEL_TRACES_SAMPLER / OTEL_TRACES_SAMPLER_ARG env vars
(set in the ConfigMap), so the ratio can be tuned without a rebuild. Must run at app creation, not
in the lifespan: FastAPIInstrumentor adds middleware, which Starlette refuses once the app started.
"""

import asyncio

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

from app.settings import get_settings
from app.telemetry import EXPORT_TIMEOUT_S, SERVICE_NAME

# Kubelet probes hit these every few seconds; without the exclusion they would dominate the
# trace list and burn through OpenObserve's small PVC. Comma-separated regexes on the URL.
EXCLUDED_URLS = "/health/liveness,/health/readiness"

_provider: TracerProvider | None = None


def setup_tracing(app: FastAPI, engine: AsyncEngine) -> None:
    global _provider
    settings = get_settings()
    _provider = TracerProvider(
        resource=Resource({"service.name": SERVICE_NAME, "k8s.pod.name": settings.pod_name}),
    )
    _provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.otel_traces_url, timeout=EXPORT_TIMEOUT_S)
        )
    )
    trace.set_tracer_provider(_provider)

    # Inbound HTTP (also propagates W3C traceparent) and SQL. There is no outbound HTTP.
    #
    # The instrumentors are given a no-op MeterProvider: left alone they publish their own
    # http_server_* / db_client_connections_* metrics through the global provider, duplicating the
    # ones in app/telemetry.py - and labelled with the pod IP and the client-controlled Host
    # header (http_host, http_server_name), which lets a caller mint new series at will.
    #
    # exclude_spans drops the per-message "http send"/"http receive" child spans, which would
    # otherwise triple the span count of every request without adding information.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=_provider,
        excluded_urls=EXCLUDED_URLS,
        exclude_spans=["receive", "send"],
        meter_provider=NoOpMeterProvider(),
    )
    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine, tracer_provider=_provider, meter_provider=NoOpMeterProvider()
    )


async def shutdown_tracing() -> None:
    if _provider is not None:
        # shutdown() flushes queued spans first (bounded by the exporter timeout).
        await asyncio.to_thread(_provider.shutdown)
