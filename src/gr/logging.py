"""structlog configuration: run/doc/stage context, no raw document text in events (NFR-002)."""

import hashlib
import logging
import sys
import traceback
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog

from gr.settings import LogFormat, LogLevel

MAX_FIELD_CHARS = 200
_CODE_FIELDS = frozenset({"event", "exception"})  # written by us, never document text


def text_fingerprint(text: str) -> dict[str, int | str]:
    """Loggable stand-in for a text: its length and a short hash, never the content."""
    return {"len": len(text), "sha256_8": hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]}


def _redact_long_strings(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    # Safety net: a long string in an event is almost certainly document or prompt text.
    for key, value in list(event_dict.items()):
        if key not in _CODE_FIELDS and isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
            event_dict[key] = {"redacted": True, **text_fingerprint(value)}
    return event_dict


def safe_traceback(exc: BaseException) -> str:
    """Stack frames and exception types of the whole chain, without the messages: a message may
    carry document or model text (a ValidationError quotes its `input_value`)."""
    chain: list[traceback.TracebackException] = []
    current: traceback.TracebackException | None = traceback.TracebackException.from_exception(exc)
    while current is not None and len(chain) < 10:
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    parts: list[str] = []
    for item in reversed(chain):
        parts.append("Traceback (most recent call last):\n")
        parts.extend(item.stack.format())
        exc_type = item.exc_type
        name = f"{exc_type.__module__}.{exc_type.__qualname__}" if exc_type else "Exception"
        parts.append(f"{name.removeprefix('builtins.')} (message withheld)\n")
    return "".join(parts)


def _safe_exc_info(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    exc_info = event_dict.pop("exc_info", None)
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, tuple):
        exc_info = exc_info[1]
    if isinstance(exc_info, BaseException):
        event_dict["exception"] = safe_traceback(exc_info)
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
            _safe_exc_info,
            _redact_long_strings,
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
