"""Hazelcast client wrapper (map `messages`, key: message id, value: JSON string of the message).

The app connects as a Hazelcast *client* to the standalone member deployed on its own node
(k8s/hazelcast-deployment.yaml), so the cache tier is independent of app pod restarts/scaling.
Unisocket mode (smart_routing=False) because there is a single member behind a ClusterIP Service.

Deliberately no Near Cache: a client-side Near Cache went stale across pods after an evict(),
because the invalidation broadcast didn't reliably reach every other client. Reads go straight to
the shared member instead. No TTL: entries are evicted after a successful update/delete.

The Python client is thread-based and blocking, so every call runs in a worker thread
(asyncio.to_thread) to keep the event loop free.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from hazelcast.client import HazelcastClient
from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode

CLUSTER_NAME = "message-service-cache"
MAP_NAME = "messages"

_tracer = trace.get_tracer("message-service")


class MessageCache(Protocol):
    async def get(self, message_id: str) -> str | None: ...
    async def set(self, message_id: str, value: str) -> None: ...
    async def evict(self, message_id: str) -> None: ...


class HazelcastCache:
    def __init__(self) -> None:
        self._client: HazelcastClient | None = None
        self._map: Any = None

    async def connect(self, host: str, port: int) -> None:
        # The cache is mandatory: if the member is unreachable, startup fails rather than
        # silently running without it. cluster_connect_timeout is bounded (the client default
        # retries for 120s) so a dead member fails well inside the ~70s startup-probe budget.
        def _connect() -> HazelcastClient:
            return HazelcastClient(
                cluster_name=CLUSTER_NAME,
                cluster_members=[f"{host}:{port}"],
                smart_routing=False,
                cluster_connect_timeout=20,
            )

        self._client = await asyncio.to_thread(_connect)
        self._map = self._client.get_map(MAP_NAME).blocking()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.lifecycle_service.is_running()

    async def _traced[T](self, operation: str, fn: Callable[[Span], Awaitable[T]]) -> T:
        # Hazelcast has no OpenTelemetry instrumentation, so each cache call gets a manual span
        # (a child of the request span). Otherwise a cache hit shows up as a request with no DB
        # spans and an unexplained gap for the network hop to the member.
        with _tracer.start_as_current_span(
            f"hazelcast.{operation}",
            attributes={
                "db.system": "hazelcast",
                "db.operation": operation,
                "cache.map": MAP_NAME,
            },
        ) as span:
            try:
                return await fn(span)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    async def get(self, message_id: str) -> str | None:
        async def run(span: Span) -> str | None:
            raw: str | None = await asyncio.to_thread(self._map.get, message_id)
            span.set_attribute("cache.hit", raw is not None)
            return raw

        return await self._traced("get", run)

    async def set(self, message_id: str, value: str) -> None:
        async def run(_: Span) -> None:
            await asyncio.to_thread(self._map.set, message_id, value)

        await self._traced("set", run)

    async def evict(self, message_id: str) -> None:
        async def run(_: Span) -> None:
            await asyncio.to_thread(self._map.delete, message_id)

        await self._traced("delete", run)

    async def shutdown(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.shutdown)
            self._client = None
            self._map = None


cache = HazelcastCache()


def get_cache() -> MessageCache:
    return cache
