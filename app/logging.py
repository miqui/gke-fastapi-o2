import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from pythonjsonlogger.json import JsonFormatter


class _Formatter(JsonFormatter):
    """One JSON object per line: timestamp, level, message, logger, plus trace_id/span_id when a
    span is active. The pod log shipper forwards stdout to OpenObserve's pod_logs stream."""

    def add_fields(
        self,
        log_data: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_data, record, message_dict)
        log_data["timestamp"] = datetime.fromtimestamp(record.created, UTC).isoformat(
            timespec="milliseconds"
        )
        log_data["level"] = record.levelname
        log_data["logger"] = record.name
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            log_data["trace_id"] = format(span_context.trace_id, "032x")
            log_data["span_id"] = format(span_context.span_id, "016x")


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Route uvicorn/alembic through the root JSON handler. uvicorn's own access log is off
    # (see app/__main__.py): app.telemetry logs one line per non-health request instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "alembic"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
