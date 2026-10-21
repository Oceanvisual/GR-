import io
import json
from collections.abc import Iterator

import pytest

from gr.logging import (
    MAX_FIELD_CHARS,
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
    text_fingerprint,
)


@pytest.fixture
def json_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging("DEBUG", "json", stream=stream)
    yield stream
    clear_context()


def _events(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_events_with_context(json_stream: io.StringIO) -> None:
    bind_context(run_id="run_1", doc_id="doc_1", stage="detail")
    get_logger("t").info("stage.done", duration_ms=12)
    (event,) = _events(json_stream)
    assert event["event"] == "stage.done"
    assert event["run_id"] == "run_1"
    assert event["stage"] == "detail"
    assert event["level"] == "info"


def test_long_strings_are_redacted(json_stream: io.StringIO) -> None:
    document_text = "Пункт 3.5.1. " * 50
    get_logger("t").info("llm.call", prompt=document_text, model="m")
    (event,) = _events(json_stream)
    assert event["prompt"] == {"redacted": True, **text_fingerprint(document_text)}
    assert all(len(value) <= MAX_FIELD_CHARS for value in event.values() if isinstance(value, str))
    assert "Пункт 3.5.1." not in json_stream.getvalue()


def test_level_filtering() -> None:
    stream = io.StringIO()
    configure_logging("WARNING", "json", stream=stream)
    get_logger("t").info("hidden")
    get_logger("t").warning("shown")
    assert [e["event"] for e in _events(stream)] == ["shown"]


def test_fingerprint_is_stable() -> None:
    assert text_fingerprint("абв") == {"len": 3, "sha256_8": text_fingerprint("абв")["sha256_8"]}
    assert text_fingerprint("абв") != text_fingerprint("абг")
