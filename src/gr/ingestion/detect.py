"""Format detection by file signature; the extension is never trusted (FR-001)."""

import io
import re
import zipfile

from gr.domain.enums import DocFormat
from gr.ingestion.base import ARCHIVE_ERRORS, IngestionError, archive_error

_HTML_RE = re.compile(rb"^\s*(<!--.*?-->\s*)*<(!doctype\s+html|html|head|body)\b", re.I | re.S)
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_WORD_STREAM = "WordDocument".encode("utf-16-le")
# Password-protected OOXML (and DOC) is an OLE container with an EncryptionInfo stream (R-04).
ENCRYPTION_STREAM = "EncryptionInfo".encode("utf-16-le")
_TEXT_PROBE = 64 * 1024

# Recognised but never accepted: images are out of MVP scope, spreadsheets are not acts.
_PDF_OBJECT_RE = re.compile(rb"\d+\s+\d+\s+obj\b")
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


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except ARCHIVE_ERRORS:
        raise archive_error() from None


def _zip_format(data: bytes) -> DocFormat:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if "mimetype" in names:
                mimetype = _read_member(archive, "mimetype")[:100].strip()
                if mimetype == b"application/vnd.oasis.opendocument.text":
                    return DocFormat.ODT
                raise unsupported(mimetype.decode("ascii", "replace") or "ODF")
    except ARCHIVE_ERRORS:
        raise archive_error() from None
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
    pdf_at = head.find(b"%PDF-")
    # Readers accept a header anywhere in the first KB. After binary junk it is a PDF; after
    # readable text (saved HTTP headers, say) only with PDF objects following it, otherwise a
    # text file merely mentions it.
    prefix = head[:pdf_at]
    if pdf_at != -1 and (
        not prefix.strip()
        or not _looks_like_text(prefix)
        or _PDF_OBJECT_RE.search(data, pdf_at, pdf_at + (64 << 10))
    ):
        return DocFormat.PDF
    if data.startswith(b"PK\x03\x04"):
        return _zip_format(data)
    if data.startswith(OLE_SIGNATURE):
        if ENCRYPTION_STREAM in data:
            raise IngestionError("encrypted_file", "Файл защищён паролем.")
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
