"""Upload anything (SC-014): every file gets a clear answer in Russian, never a 500.

Unsupported containers, images, random bytes, empty and whitespace files, unknown encodings
and damaged documents go through the real API and worker process. A file that is accepted
must also render its structure and its page.
"""

import io
import random
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from mutations import FIXTURES, mutate_bytes

from gr.app import create_app
from gr.errors import ERRORS
from gr.settings import Settings

URL = "/api/v1/documents"
OK = {200, 201}


def zip_bytes(entries: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def ole(stream_name: str) -> bytes:
    """Just enough of a compound file for the signature and a stream name to be found."""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504) + stream_name.encode("utf-16-le")


CASES: dict[str, tuple[str, bytes, set[str]]] = {
    "zip": ("archive.zip", zip_bytes({"a.txt": "текст"}), {"unsupported_format"}),
    "xlsx": (
        "table.xlsx",
        zip_bytes({"[Content_Types].xml": "<x/>", "xl/workbook.xml": "<w/>"}),
        {"unsupported_format"},
    ),
    "pptx": (
        "slides.pptx",
        zip_bytes({"[Content_Types].xml": "<x/>", "ppt/presentation.xml": "<p/>"}),
        {"unsupported_format"},
    ),
    "odt": (
        "text.odt",
        zip_bytes({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": "<c/>"}),
        {"unsupported_format"},
    ),
    "epub": (
        "book.epub",
        zip_bytes({"mimetype": "application/epub+zip", "content.opf": "<o/>"}),
        {"unsupported_format"},
    ),
    "docx_without_body": (
        "empty.docx",
        zip_bytes({"word/styles.xml": "<s/>"}),
        {"unsupported_format"},
    ),
    "png": ("scan.png", b"\x89PNG\r\n\x1a\n" + bytes(64), {"unsupported_format"}),
    "jpeg": ("photo.jpg", b"\xff\xd8\xff\xe0" + bytes(64), {"unsupported_format"}),
    "tiff": ("scan.tiff", b"II*\x00" + bytes(64), {"unsupported_format"}),
    "gif": ("anim.gif", b"GIF89a" + bytes(64), {"unsupported_format"}),
    "webp": ("image.webp", b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(64), {"unsupported_format"}),
    "exe": ("setup.exe", b"MZ\x90\x00" + bytes(256), {"unsupported_format"}),
    "doc": ("old.doc", ole("WordDocument"), {"unsupported_format"}),
    "xls_ole": ("old.xls", ole("Workbook"), {"unsupported_format"}),
    "encrypted_ole": ("secret.docx", ole("EncryptionInfo"), {"encrypted_file"}),
    "rtf": ("text.rtf", rb"{\rtf1\ansi Hello}", {"unsupported_format"}),
    "html_without_text": (
        "page.html",
        "<html><body><nav>Меню</nav><script>var x = 1;</script></body></html>".encode(),
        {"empty_document"},
    ),
    "empty": ("empty.txt", b"", {"empty_document"}),
    "whitespace": ("blank.txt", b" \n\t\r\n  ", {"empty_document"}),
    "invisible_only": ("zw.txt", "\u200b\ufeff\u2060".encode(), {"empty_document"}),
    "shift_jis": (
        "japanese.txt",
        ("日本語の文書です。" * 40).encode("shift_jis"),
        {"encoding_undetected"},
    ),
    "high_bytes": (
        "noise.txt",
        bytes(random.Random(1).randrange(0x80, 0x100) for _ in range(2000)),
        {"encoding_undetected", "unsupported_format"},
    ),
    "pdf_header_only": ("stub.pdf", b"%PDF-1.7\n", {"corrupted_file"}),
    **{
        f"random_{seed}": (
            "random.bin",
            random.Random(seed).randbytes(random.Random(seed).randint(1, 50_000)),
            {"unsupported_format", "corrupted_file", "encoding_undetected"},
        )
        for seed in range(5)
    },
}


@pytest.fixture
def client(tmp_path: Path, make_settings: Callable[..., Settings]) -> Iterator[TestClient]:
    settings = make_settings(data_dir=tmp_path / "data", ocr_enabled=False)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def upload(client: TestClient, name: str, data: bytes) -> httpx2.Response:
    return client.post(URL, files={"file": (name, data)})


def assert_clear_answer(client: TestClient, response: httpx2.Response, context: str) -> None:
    assert response.status_code != 500, context
    if response.status_code in OK:
        doc_id = response.json()["id"]
        assert client.get(f"{URL}/{doc_id}/structure").status_code == 200, context
        assert client.get(f"/documents/{doc_id}").status_code == 200, context
        return
    error = response.json()["error"]
    assert error["code"] in ERRORS and error["code"] != "internal_error", context
    assert response.status_code == ERRORS[error["code"]][0], context
    assert any("а" <= char <= "я" for char in error["message"].lower()), context


@pytest.mark.parametrize("case", CASES)
def test_any_file_gets_a_clear_answer(client: TestClient, case: str) -> None:
    name, data, codes = CASES[case]
    response = upload(client, name, data)
    assert_clear_answer(client, response, case)
    assert response.json()["error"]["code"] in codes, response.text
    if response.status_code == 415:
        assert response.json()["error"]["details"]["supported"] == ["docx", "html", "pdf", "txt"]


def test_utf16_text_is_accepted(client: TestClient) -> None:
    text = (FIXTURES / "synthetic" / "regulation.txt").read_text()
    response = upload(client, "utf16.txt", text.encode("utf-16"))
    assert response.status_code in OK, response.text
    assert_clear_answer(client, response, "utf-16")


# Damaged real documents through the worker process: refused clearly or parsed and rendered.
DAMAGED = [
    (path, seed)
    for path in (FIXTURES / "npa" / "reg261.docx", FIXTURES / "npa" / "reg261.pdf")
    for seed in range(4)
]


@pytest.mark.parametrize(("path", "seed"), DAMAGED, ids=lambda v: getattr(v, "name", v))
def test_damaged_documents_through_api(client: TestClient, path: Path, seed: int) -> None:
    data, kind = mutate_bytes(path.read_bytes(), seed)
    response = upload(client, f"damaged{path.suffix}", data)
    assert_clear_answer(client, response, f"{path.name} seed={seed} kind={kind}")
