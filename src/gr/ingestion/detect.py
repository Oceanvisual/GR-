"""Format detection by file signature; the extension is never trusted (FR-001)."""

import io
import re
import zipfile

from gr.domain.enums import DocFormat
from gr.ingestion.base import IngestionError

_HTML_RE = re.compile(rb"^\s*(<!--.*?-->\s*)*<(!doctype\s+html|html|head|body)\b", re.I | re.S)
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_WORD_STREAM = "WordDocument".encode("utf-16-le")
_TEXT_PROBE = 64 * 1024

# Recognised but never accepted: images are out of MVP scope, spreadsheets are not acts.
_KNOWN_UNSUPPORTED = {
    b"\x89PNG": "PNG",
    b"\xff\xd8\xff": "JPEG",
    b"II*\x00": "TIFF",
    b"MM\x00*": "TIFF",
    b"GIF8": "GIF",
}


def unsupported(kind: str) -> IngestionError:
    return IngestionError(
        "unsupported_format",
        f"Формат файла не поддерживается ({kind}).",
        {"detected": kind},
    )


def _zip_format(data: bytes) -> DocFormat:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if "mimetype" in names:
                mimetype = archive.read("mimetype")[:100].strip()
                if mimetype == b"application/vnd.oasis.opendocument.text":
                    return DocFormat.ODT
                raise unsupported(mimetype.decode("ascii", "replace") or "ODF")
    except zipfile.BadZipFile:
        raise IngestionError("corrupted_file", "Файл повреждён: архив не читается.") from None
    if "word/document.xml" in names:
        return DocFormat.DOCX
    if any(name.startswith("xl/") for name in names):
        raise unsupported("XLSX")
    if any(name.startswith("ppt/") for name in names):
        raise unsupported("PPTX")
    raise unsupported("ZIP")


def _looks_like_text(data: bytes) -> bool:
    probe = data[:_TEXT_PROBE]
    if probe.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True  # UTF-16 with BOM
    if b"\x00" in probe:
        return False
    control = sum(1 for byte in probe if byte < 0x20 and byte not in b"\t\n\r\f\x0b")
    return control <= len(probe) * 0.01


def detect_format(data: bytes) -> DocFormat:
    """Signature → format. Unknown or binary content raises `unsupported_format`."""
    if not data.strip():
        raise IngestionError("empty_document", "Файл пустой.")
    head = data[:1024]
    if b"%PDF-" in head:
        return DocFormat.PDF
    if data.startswith(b"PK\x03\x04"):
        return _zip_format(data)
    if data.startswith(_OLE_SIGNATURE):
        if _WORD_STREAM in data:
            return DocFormat.DOC
        raise unsupported("OLE (не Word)")
    if head.lstrip().startswith(b"{\\rtf"):
        return DocFormat.RTF
    for signature, kind in _KNOWN_UNSUPPORTED.items():
        if data.startswith(signature):
            raise unsupported(kind)
    if _looks_like_text(data):
        without_bom = data.removeprefix(b"\xef\xbb\xbf")
        return DocFormat.HTML if _HTML_RE.match(without_bom[:4096]) else DocFormat.TXT
    raise unsupported("двоичный файл")
