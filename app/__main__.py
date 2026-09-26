"""Server entrypoint: `python -m app`.

Runs uvicorn programmatically (rather than the `uvicorn` CLI) for two shutdown-contract reasons:

1. Readiness must go DOWN when SIGTERM arrives, *before* the listener closes, and the pod keeps
   serving for `shutdown_drain_seconds` so the Service/Ingress stop routing to it. Uvicorn's own
   handler closes the listener immediately.
2. Uvicorn re-raises a captured SIGTERM after a graceful shutdown, so the process dies by signal
   (exit code 143). The contract is a clean exit 0.
"""

import asyncio
import contextlib
import logging
import signal
import threading
from collections.abc import Generator
from types import FrameType

import uvicorn

from app.logging import setup_logging
from app.settings import get_settings
from app.state import state

log = logging.getLogger("app")


class GracefulServer(uvicorn.Server):
    drain_seconds: float = 0.0
    _draining = False
    _loop: asyncio.AbstractEventLoop | None = None

    async def serve(self, sockets: list | None = None) -> None:  # type: ignore[type-arg, override]
        self._loop = asyncio.get_running_loop()
        await super().serve(sockets)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        state.ready = False  # step 1: readiness DOWN
        if self._draining or self._loop is None or self.drain_seconds <= 0:
            super().handle_exit(sig, frame)  # a second signal, or no drain: stop now
            return
        self._draining = True
        log.info("%s received, draining for %ss", signal.Signals(sig).name, self.drain_seconds)
        self._loop.call_soon_threadsafe(
            self._loop.call_later, self.drain_seconds, super().handle_exit, sig, frame
        )

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {
            sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)
        # Unlike the base class, do not re-raise the captured signal: exit code 0.


def main() -> None:
    settings = get_settings()
    setup_logging()
    config = uvicorn.Config(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container listens on all interfaces
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=20,
        access_log=False,  # app.telemetry logs one line per non-health request
        log_config=None,  # keep the JSON logging configured above
    )
    server = GracefulServer(config)
    server.drain_seconds = settings.shutdown_drain_seconds
    server.run()


if __name__ == "__main__":
    main()
