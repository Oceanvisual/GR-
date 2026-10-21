"""Extractor protocol and ingestion errors shared by every format adapter (FR-007)."""

import zipfile
import zlib
from typing import Any, Protocol

from gr.domain.document import RawDocument
from gr.domain.enums import DocFormat

# Codes from the api.md error table that ingestion can raise.
INGESTION_CODES = frozenset(
    {
        "unsupported_format",
        "corrupted_file",
        "encrypted_file",
        "empty_document",
        "encoding_undetected",
        "document_too_long",
        "file_too_large",
    }
)


# What a damaged ZIP container raises while opening or reading a member: a broken central
# directory (ValueError "negative seek"), a corrupt deflate stream (zlib.error), a truncated
# member (EOFError), an encrypted or exotic member (RuntimeError, NotImplementedError).
ARCHIVE_ERRORS: tuple[type[Exception], ...] = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    EOFError,
    OSError,
    ValueError,
    KeyError,
    RuntimeError,
    NotImplementedError,
)


def archive_error() -> "IngestionError":
    return IngestionError("corrupted_file", "Файл повреждён: архив не читается.")


class IngestionError(Exception):
    """A file the service cannot read; the API turns it into the same error code."""

    def __init__(self, code: str, message_ru: str, details: dict[str, Any] | None = None) -> None:
        if code not in INGESTION_CODES:
            raise ValueError(f"unknown ingestion error code {code!r}")
        super().__init__(f"{code}: {message_ru}")
        self.code = code
        self.message_ru = message_ru
        self.details = details or {}

    def __reduce__(self) -> tuple[Any, ...]:
        # Picklable across the worker process boundary (T076).
        return (IngestionError, (self.code, self.message_ru, self.details))


class Extractor(Protocol):
    """Turns file bytes into IR blocks. Runs inside a worker process, so it is synchronous."""

    format: DocFormat

    def extract(self, data: bytes) -> RawDocument: ...
