"""structlog configuration: run/doc/stage context, no raw document text in events (NFR-002)."""

import hashlib
import logging
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog

from gr.settings import LogFormat, LogLevel

MAX_FIELD_CHARS = 200


def text_fingerprint(text: str) -> dict[str, int | str]:
    """Loggable stand-in for a text: its length and a short hash, never the content."""
    return {"len": len(text), "sha256_8": hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]}


def _redact_long_strings(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    # Safety net: a long string in an event is almost certainly document or prompt text.
    for key, value in list(event_dict.items()):
        if key != "event" and isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
            event_dict[key] = {"redacted": True, **text_fingerprint(value)}
    return event_dict


def configure_logging(
    level: LogLevel = "INFO", fmt: LogFormat = "console", stream: TextIO | None = None
) -> None:
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_long_strings,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


def bind_context(**values: str) -> None:
    """Bind run_id / doc_id / stage for every event emitted in the current context."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
