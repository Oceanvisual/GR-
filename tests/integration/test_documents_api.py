"""Documents API end to end: upload, refusals, structure, fragments, metadata (T022, Q1–Q3)."""

import asyncio
import io
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from fpdf import FPDF
from fpdf.enums import EncryptionMethod

from gr.app import create_app
from gr.services.documents import safe_filename
from gr.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NPA = FIXTURES / "npa"
SYNTHETIC = FIXTURES / "synthetic"
URL = "/api/v1/documents"


@pytest.fixture
def settings(tmp_path: Path, make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(data_dir=tmp_path / "data", ocr_enabled=False, max_upload_mb=20)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def upload(client: TestClient, path: Path | None = None, *, name: str | None = None,
           data: bytes | None = None, **form: str) -> httpx2.Response:  # fmt: skip
    content = data if data is not None else (path.read_bytes() if path else b"")
    filename = name or (path.name if path else "file.bin")
    return client.post(URL, files={"file": (filename, content)}, data=form)


def error_code(response: httpx2.Response) -> str:
    code: str = response.json()["error"]["code"]
    return code


# --- Q1: DOCX upload, requisites, structure ----------------------------------------------


def test_q1_docx_upload_structure_and_requisites(client: TestClient) -> None:
    response = upload(client, NPA / "reg261.docx", source_note="сайт ТУ Росимущества")
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["id"].startswith("doc_") and document["format"] == "docx"
    assert document["user_source"] == "сайт ТУ Росимущества"
    metadata = document["metadata"]
    assert metadata["date"]["value"] == "11.09.2020" and metadata["number"]["value"] == "261"
    assert metadata["edition_date"]["value"] == "30.05.2023"
    assert metadata["minjust_registration"]["value"] == "14.09.2020 № 59820"
    assert metadata["date"]["citation"]["quote"] in ("11 сентября 2020", "11.09.2020")
    assert document["diagnostics"]["structure"]["strategy"] == "regulation"
    assert document["diagnostics"]["analyzable"] is True

    tree = client.get(f"{URL}/{document['id']}/structure").json()
    assert tree["mode"] == "numbered"
    assert tree["stats"]["points"] == 115 and tree["stats"]["sections"] == 6
    labels = {node["label"] for node in tree["nodes"]}
    assert {"разд. I", "разд. V", "п. 2.6", "п. 2.16", "п. 3.8.3"} <= labels
    point = next(node for node in tree["nodes"] if node["label"] == "п. 2.16")
    assert len(point["children"]) >= 26 and "text" not in point
    assert point["text_preview"].startswith("2.16.")


def test_fragment_with_context(client: TestClient) -> None:
    doc_id = upload(client, NPA / "reg261.docx").json()["id"]
    tree = client.get(f"{URL}/{doc_id}/structure").json()
    node = next(n for n in tree["nodes"] if n["label"] == "п. 3.5.1")
    plain = client.get(f"{URL}/{doc_id}/fragments/{node['id']}").json()
    assert plain["fragment"]["text"].startswith("3.5.1. Специалист") and plain["parent"] is None
    context = client.get(f"{URL}/{doc_id}/fragments/{node['id']}", params={"with_context": True})
    body = context.json()
    assert body["parent"]["label"] == "п. 3.5"
    assert body["next"]["label"] == "п. 3.5.2" and body["previous"] is None
    missing = client.get(f"{URL}/{doc_id}/fragments/body/nope")
    assert missing.status_code == 404 and error_code(missing) == "not_found"


def test_duplicate_upload_returns_existing(client: TestClient) -> None:
    first = upload(client, NPA / "reg261.docx")
    second = upload(client, NPA / "reg261.docx", name="копия.docx")
    assert second.status_code == 200
    assert second.headers["X-Duplicate-Of"] == first.json()["id"] == second.json()["id"]
    assert len(client.get(URL).json()) == 1


# --- Q2: PDF with pages ------------------------------------------------------------------


def test_q2_pdf_pages(client: TestClient) -> None:
    document = upload(client, NPA / "reg261.pdf").json()
    assert document["page_count"] == 47
    tree = client.get(f"{URL}/{document['id']}/structure").json()
    node = next(n for n in tree["nodes"] if n["label"] == "п. 3.5.1")
    assert node["pages"] and all(1 <= p <= 47 for p in node["pages"])
    assert tree["stats"]["pages"] == 47


# --- Q3: refusals ------------------------------------------------------------------------


def test_q3_scan_is_kept_but_not_analyzable(client: TestClient) -> None:
    response = upload(client, SYNTHETIC / "scan.pdf")
    assert response.status_code == 201
    diagnostics = response.json()["diagnostics"]
    assert diagnostics["text_layer"] == "needs_ocr" and diagnostics["empty_pages"] == [1]
    assert diagnostics["analyzable"] is False
    assert diagnostics["ocr"]["status"] == "unavailable" and diagnostics["ocr"]["error"]


def _xlsx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<x/>")
        archive.writestr("xl/workbook.xml", "<w/>")
    return buffer.getvalue()


def _encrypted_pdf() -> bytes:
    document = FPDF()
    document.add_page()
    document.set_font("helvetica", size=12)
    document.cell(text="secret")
    document.set_encryption(
        owner_password="o", user_password="u", encryption_method=EncryptionMethod.RC4
    )
    return bytes(document.output())


@pytest.mark.parametrize(
    ("name", "data", "status", "code"),
    [
        ("corrupted.pdf", (SYNTHETIC / "corrupted.pdf").read_bytes(), 422, "corrupted_file"),
        ("report.txt", _xlsx(), 415, "unsupported_format"),
        ("page.html", (SYNTHETIC / "regulation.html").read_bytes(), 415, "unsupported_format"),
        ("secret.pdf", _encrypted_pdf(), 422, "encrypted_file"),
        ("empty.txt", b"", 422, "empty_document"),
        ("blank.txt", b"   \n\n", 422, "empty_document"),
    ],
)
def test_q3_refusals(client: TestClient, name: str, data: bytes, status: int, code: str) -> None:
    response = upload(client, name=name, data=data)
    assert response.status_code == status, response.text
    assert error_code(response) == code
    if code == "unsupported_format":
        assert response.json()["error"]["details"]["supported"] == ["docx", "pdf", "txt"]
    assert client.get(URL).json() == []


def test_q3_too_large_by_content_length(settings: Settings) -> None:
    small = settings.model_copy(update={"max_upload_mb": 1})
    with TestClient(create_app(small)) as client:
        response = upload(client, name="big.txt", data=b"a" * (3 << 20))
        assert response.status_code == 413 and error_code(response) == "file_too_large"


def test_too_large_stream_without_content_length(settings: Settings) -> None:
    small = settings.model_copy(update={"max_upload_mb": 1})
    boundary = "xyz"

    def body() -> Iterator[bytes]:
        yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
               "filename=\"big.txt\"\r\n\r\n").encode()  # fmt: skip
        for _ in range(64):
            yield b"a" * (64 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    with TestClient(create_app(small)) as client:
        response = client.post(
            URL,
            content=body(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert response.status_code == 413 and error_code(response) == "file_too_large"
        assert not list((small.data_dir / "tmp").glob(".incoming-*"))


def test_missing_file_field(client: TestClient) -> None:
    response = client.post(URL, data={"source_note": "x"}, files={"other": ("a.txt", b"x")})
    assert response.status_code == 400 and error_code(response) == "validation_error"
    response = client.post(URL, json={"file": "x"})
    assert response.status_code == 400


def test_filename_is_sanitized(client: TestClient) -> None:
    document = upload(client, SYNTHETIC / "regulation.txt", name="../../etc/reg.txt").json()
    assert document["filename"] == "reg.txt"


@pytest.mark.parametrize(
    ("raw", "safe"),
    [("..\\..\\a.docx", "a.docx"), ("a\x07b.pdf", "ab.pdf"), ("", "document"), ("..", "document")],
)
def test_safe_filename(raw: str, safe: str) -> None:
    assert safe_filename(raw) == safe
    assert len(safe_filename("x" * 400)) == 255


# --- list, card, metadata edits ----------------------------------------------------------


def test_list_and_card(client: TestClient) -> None:
    doc_id = upload(client, SYNTHETIC / "regulation.txt").json()["id"]
    [summary] = client.get(URL).json()
    assert summary["id"] == doc_id and summary["text_layer"] == "ok"
    assert summary["title"].startswith("Об утверждении") and summary["last_run"] is None
    assert client.get(f"{URL}/{doc_id}").json()["id"] == doc_id
    assert client.get(f"{URL}/doc_missing").status_code == 404


def test_patch_metadata_marks_user_values(client: TestClient) -> None:
    doc_id = upload(client, SYNTHETIC / "regulation.txt").json()["id"]
    patch: dict[str, Any] = {
        "fields": {"edition_date": "01.02.2024", "issuer": None},
        "user_source": "официальный сайт",
        "relevance_checked_at": "2026-10-21",
    }
    document = client.patch(f"{URL}/{doc_id}/metadata", json=patch).json()
    assert document["metadata"]["edition_date"] == {
        "value": "01.02.2024", "citation": None, "source": "user"
    }  # fmt: skip
    assert document["metadata"]["issuer"]["value"] is None
    assert document["metadata"]["date"]["source"] == "document"  # untouched fields stay
    assert document["user_source"] == "официальный сайт"
    assert document["relevance_checked_at"] == "2026-10-21"
    bad = client.patch(f"{URL}/{doc_id}/metadata", json={"fields": {"unknown": "x"}})
    assert bad.status_code == 400


# --- performance and isolation -----------------------------------------------------------


def _long_pdf(pages: int) -> bytes:
    document = FPDF(format="A4")
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    font = next(p for p in candidates if p.exists())
    document.add_font("Main", fname=str(font))
    document.set_font("Main", size=11)
    for page in range(1, pages + 1):
        document.add_page()
        document.multi_cell(
            0, 6, f"{page}. Пункт номер {page}. " + "Текст пункта регламента. " * 120
        )
    return bytes(document.output())


@pytest.mark.slow
async def test_large_pdf_fast_and_server_stays_responsive(settings: Settings) -> None:
    app = create_app(settings)
    data = await asyncio.to_thread(_long_pdf, 100)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as client,
    ):
        started = time.perf_counter()
        uploading = asyncio.create_task(client.post(URL, files={"file": ("long.pdf", data)}))
        await asyncio.sleep(0.3)
        latencies = []
        while not uploading.done():
            ping = time.perf_counter()
            assert (await client.get("/healthz")).status_code == 200
            latencies.append(time.perf_counter() - ping)
            await asyncio.sleep(0.05)
        response = await uploading
        elapsed = time.perf_counter() - started
    assert response.status_code == 201, response.text
    assert response.json()["page_count"] == 100
    assert elapsed <= 30  # SC-001
    assert latencies and max(latencies) <= 0.2
