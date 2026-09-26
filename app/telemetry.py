"""Metrics: OTLP/HTTP push to the collector every 15s (there is no /metrics scrape endpoint).

Resource attributes service.name and k8s.pod.name become the Prometheus labels service_name and
k8s_pod_name (the collector has resource_to_telemetry_conversion on) - so they are NOT repeated as
data-point attributes. No `unit` is set on any instrument: the collector's Prometheus exporter
appends a unit-derived suffix ("ms" -> "_milliseconds") unless the name already ends in exactly
that, which would double-suffix names that already spell out `_ms` / `_bytes`.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

import psutil
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import problem_response
from app.settings import get_settings

log = logging.getLogger("app.access")

SERVICE_NAME = "message-service"
# Bounds every export attempt (and so shutdown): an unreachable collector must not hold the pod
# past terminationGracePeriodSeconds.
EXPORT_TIMEOUT_S = 5

# Instruments come from the global meter: no-ops until setup_metrics() installs a provider, so
# the middleware works unchanged in tests.
_meter = metrics.get_meter(SERVICE_NAME)
_requests = _meter.create_counter(
    "http_requests_total", description="HTTP requests handled, by method, route template and status"
)
_errors = _meter.create_counter(
    "http_errors_total", description="HTTP error responses, by method, route template and code"
)
_duration = _meter.create_histogram(
    "http_request_duration_ms", description="HTTP request duration in milliseconds"
)

# Kubelet probes hit these every few seconds; they would drown the real traffic.
EXCLUDED_PREFIX = "/health/"
UNMATCHED_ROUTE = "unmatched"


class RequestMetricsMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware: it breaks contextvars and streaming).

    Also the last line of defence for unhandled exceptions: logs them with the active trace id and
    answers a generic problem+json 500, so no stack trace ever reaches the client.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(EXCLUDED_PREFIX):
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status = 500
        started = False

        async def send_wrapper(message: Any) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                status = message["status"]
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled exception", extra={"method": scope["method"]})
            status = 500
            if not started:
                response = problem_response(
                    scope, 500, "INTERNAL_SERVER_ERROR", "An unexpected error occurred."
                )
                await response(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            route = getattr(scope.get("route"), "path", UNMATCHED_ROUTE)
            attrs = {"method": scope["method"], "route": route}
            _duration.record(elapsed_ms, attrs)
            _requests.add(1, {**attrs, "status_code": status})
            if status >= 400:
                _errors.add(1, {**attrs, "error_code": scope.get("error_code", "UNKNOWN")})
            log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "route": route,
                    "status_code": status,
                    "duration_ms": round(elapsed_ms, 2),
                },
            )


class _LoopLagMonitor:
    """Samples how late a 20ms sleep wakes up: the event-loop saturation signal for asyncio."""

    INTERVAL = 0.02

    def __init__(self) -> None:
        self._samples: deque[float] = deque(maxlen=3000)  # ~60s of history
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            before = time.perf_counter()
            await asyncio.sleep(self.INTERVAL)
            self._samples.append(max((time.perf_counter() - before - self.INTERVAL) * 1000, 0.0))

    def p99(self) -> float:
        if not self._samples:
            return 0.0
        ordered = sorted(self._samples)
        return ordered[int(0.99 * (len(ordered) - 1))]


class _QueryStats:
    """Average SQL statement duration since the previous collection."""

    def __init__(self) -> None:
        self._total_ms = 0.0
        self._count = 0

    def add(self, ms: float) -> None:
        self._total_ms += ms
        self._count += 1

    def take_average(self) -> float:
        avg = self._total_ms / self._count if self._count else 0.0
        self._total_ms, self._count = 0.0, 0
        return avg


_provider: MeterProvider | None = None
_lag = _LoopLagMonitor()


def setup_metrics(engine: AsyncEngine) -> None:
    global _provider
    settings = get_settings()
    # Resource() rather than Resource.create(): no telemetry.sdk.* attributes, which the
    # collector would turn into extra labels on every series.
    resource = Resource({"service.name": SERVICE_NAME, "k8s.pod.name": settings.pod_name})
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=settings.otel_metrics_url, timeout=EXPORT_TIMEOUT_S),
        export_interval_millis=15000,
    )
    _provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(_provider)
    meter = _provider.get_meter(SERVICE_NAME)

    def gauge(name: str, description: str, read: Callable[[], float]) -> None:
        def callback(_: CallbackOptions) -> Iterable[Observation]:
            yield Observation(read())

        meter.create_observable_gauge(name, callbacks=[callback], description=description)

    process = psutil.Process()
    gauge(
        "python_process_memory_rss_bytes",
        "Resident set size",
        lambda: float(process.memory_info().rss),
    )

    last = {"cpu": sum(process.cpu_times()[:2]), "wall": time.monotonic()}

    def cpu_ratio() -> float:
        cpu, wall = sum(process.cpu_times()[:2]), time.monotonic()
        elapsed = wall - last["wall"]
        ratio = (cpu - last["cpu"]) / elapsed if elapsed > 0 else 0.0
        last["cpu"], last["wall"] = cpu, wall
        return ratio

    gauge(
        "python_process_cpu_usage_ratio",
        "Process CPU usage as a fraction of one core since the last sample",
        cpu_ratio,
    )

    _lag.start()
    gauge(
        "python_eventloop_lag_p99_ms",
        "asyncio event loop scheduling delay, 99th percentile over ~60s, milliseconds",
        _lag.p99,
    )

    # DB pool + query timing, read from the SQLAlchemy pool and cursor-execute events.
    pool: Any = engine.sync_engine.pool
    gauge(
        "db_pool_connections_open",
        "Connections currently open (busy + idle)",
        lambda: float(pool.checkedin() + pool.checkedout()),
    )
    gauge("db_pool_connections_busy", "Connections checked out", lambda: float(pool.checkedout()))
    gauge(
        "db_pool_connections_idle", "Connections idle in the pool", lambda: float(pool.checkedin())
    )

    stats = _QueryStats()

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(
        conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, many: Any
    ) -> None:
        context._query_started = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after(
        conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, many: Any
    ) -> None:
        stats.add((time.perf_counter() - context._query_started) * 1000)

    gauge(
        "db_client_queries_duration_avg_ms",
        "Average SQL statement duration since the previous collection, milliseconds",
        stats.take_average,
    )


async def shutdown_metrics() -> None:
    await _lag.stop()
    if _provider is not None:
        # shutdown() performs a final collect + export, so no separate force_flush is needed.
        await asyncio.to_thread(_provider.shutdown, EXPORT_TIMEOUT_S * 1000)
