"""Plain text: encoding detection and paragraphs as IR blocks (FR-004)."""

import re

from charset_normalizer import from_bytes

from gr.domain.document import Block, IngestionDiagnostics, RawDocument
from gr.domain.enums import DocFormat, TextLayerStatus
from gr.ingestion.base import IngestionError

# Russian acts arrive in these; limiting the search keeps short cp1251 files from being read as
# Big5 or another far-fetched encoding.
CANDIDATE_ENCODINGS = ["utf_8", "cp1251", "koi8_r", "cp866", "mac_cyrillic", "iso8859_5"]
MAX_CHAOS = 0.2
MIN_LETTER_SHARE = 0.5
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")


def decode_text(data: bytes) -> tuple[str, str]:
    """Bytes → (text, encoding name). Raises `encoding_undetected` when unsure."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="strict"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    best = from_bytes(data, cp_isolation=CANDIDATE_ENCODINGS).best()
    if best is not None and best.chaos <= MAX_CHAOS:
        text = str(best)
        visible = [ch for ch in text if not ch.isspace()]
        letters = sum(1 for ch in visible if ch.isalpha())
        if visible and letters / len(visible) >= MIN_LETTER_SHARE:
            return text, best.encoding
    raise IngestionError(
        "encoding_undetected",
        "Не удалось определить кодировку текста. Сохраните файл в UTF-8.",
    )


def _is_hard_wrapped(groups: list[list[str]]) -> bool:
    """Paragraphs separated by blank lines and wrapped at a fixed width inside."""
    multi_line = [group for group in groups if len(group) > 1]
    if len(groups) < 3 or len(multi_line) < 0.5 * len(groups):
        return False
    widths = [len(line) for group in multi_line for line in group[:-1]]
    return max(widths) <= 100


def _join_wrapped(lines: list[str]) -> str:
    text = lines[0]
    for line in lines[1:]:
        if re.search(r"\w-$", text) and line[:1].islower():
            text = text[:-1] + line  # syllable hyphen at the wrap
        else:
            text = f"{text} {line}"
    return text


def split_paragraphs(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    groups = [
        [line.strip() for line in chunk.split("\n") if line.strip()]
        for chunk in _BLANK_LINE_RE.split(text)
    ]
    groups = [group for group in groups if group]
    if _is_hard_wrapped(groups):
        return [_join_wrapped(group) for group in groups]
    return [line for group in groups for line in group]


class TxtExtractor:
    format = DocFormat.TXT

    def extract(self, data: bytes) -> RawDocument:
        text, encoding = decode_text(data)
        paragraphs = split_paragraphs(text)
        if not paragraphs:
            raise IngestionError("empty_document", "В документе нет текста.")
        return RawDocument(
            format=DocFormat.TXT,
            blocks=[Block(text=paragraph) for paragraph in paragraphs],
            diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK, encoding=encoding),
        )
