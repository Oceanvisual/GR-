"""OCR in the upload flow (FR-008, R-04, T070): a scan is stored at once and recognised in the
background, the progress shows, a failure can be retried, a stop is resumed on the next start.

tesseract is a stand-in on PATH: it answers the startup probe (`--list-langs`, `--version`) and
recognises any page as the same lines. A file `hold` next to it keeps it busy, `fail` makes it
fail; so the whole path runs as in production, worker processes included.
"""

import stat
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient

from gr.app import create_app
from gr.settings import Settings

SYNTHETIC = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
URL = "/api/v1/documents"
HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)
LINES = [
    "I. Общие положения",
    "1.1. Пропуск выдаётся в течение пяти рабочих дней со дня обращения.",
    "1.2. Заявление подаётся лично или по почте.",
]


def _tsv() -> str:
    rows = [HEADER]
    for n, line in enumerate(LINES, start=1):
        for k, word in enumerate(line.split(), start=1):
            left = 60 + 40 * (k - 1)
            rows.append(f"5\t1\t1\t1\t{n}\t{k}\t{left}\t{80 + 30 * n}\t36\t14\t91.5\t{word}")
    return "\n".join(rows) + "\n"


STAND_IN = f"""#!{sys.executable}
import pathlib, sys, time
here = pathlib.Path(__file__).parent
if "--list-langs" in sys.argv:
    print("List of available languages in /stand-in (2):")
    print("eng")
    print("rus")
    sys.exit(0)
if "--version" in sys.argv:
    print("tesseract 5.3.0 (stand-in)")
    sys.exit(0)
sys.stdin.buffer.read()
deadline = time.time() + 60
while (here / "hold").exists() and time.time() < deadline:
    time.sleep(0.05)
if (here / "fail").exists():
    sys.stderr.write("Error: the page cannot be read")
    sys.exit(1)
sys.stdout.write({_tsv()!r})
"""


@pytest.fixture
def tesseract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The directory of the stand-in: `hold` and `fail` files in it steer it."""
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "tesseract"
    script.write_text(STAND_IN, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    return folder


@pytest.fixture
def settings(tmp_path: Path, make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(data_dir=tmp_path / "data", ocr_enabled=True, ocr_dpi=72,
                         ocr_page_timeout_s=30)  # fmt: skip


@pytest.fixture
def client(settings: Settings, tesseract: Path) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def upload(client: TestClient, path: Path, name: str | None = None) -> httpx2.Response:
    return client.post(URL, files={"file": (name or path.name, path.read_bytes())})


def settled(client: TestClient, doc_id: str, timeout_s: float = 60) -> dict[str, Any]:
    """The document once its OCR is over."""
    deadline = time.monotonic() + timeout_s
    while True:
        document: dict[str, Any] = client.get(f"{URL}/{doc_id}").json()
        if document["diagnostics"]["ocr"]["status"] not in ("queued", "running"):
            return document
        assert time.monotonic() < deadline, document["diagnostics"]["ocr"]
        time.sleep(0.1)


def until_running(client: TestClient, doc_id: str, timeout_s: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        document: dict[str, Any] = client.get(f"{URL}/{doc_id}").json()
        if document["diagnostics"]["ocr"]["status"] == "running":
            return document
        assert time.monotonic() < deadline, document["diagnostics"]["ocr"]
        time.sleep(0.05)


def mixed_pdf(path: Path) -> Path:
    """The text pages of the synthetic regulation, then the scanned page."""
    merged = pdfium.PdfDocument.new()
    for source in ("regulation.pdf", "scan.pdf"):
        document = pdfium.PdfDocument(str(SYNTHETIC / source))
        merged.import_pages(document)
        document.close()
    merged.save(str(path))
    merged.close()
    return path


# --- the scenarios of T070 ---------------------------------------------------------------


def test_a_scan_is_recognised_in_the_background_and_can_be_analysed(client: TestClient) -> None:
    response = upload(client, SYNTHETIC / "scan.pdf")
    assert response.status_code == 202, response.text  # stored, recognised in the background
    queued = response.json()["diagnostics"]
    assert queued["ocr"]["status"] == "queued" and queued["ocr"]["progress"] == {
        "done": 0,
        "total": 1,
    }
    assert queued["analyzable"] is False

    document = settled(client, response.json()["id"])
    diagnostics = document["diagnostics"]
    ocr = diagnostics["ocr"]
    assert ocr["status"] == "done" and ocr["pages"] == [1], ocr
    assert ocr["mean_confidence"] == 91.5 and ocr["engine_version"] == "tesseract 5.3.0 (stand-in)"
    assert ocr["progress"] == {"done": 1, "total": 1}
    assert diagnostics["text_layer"] == "needs_ocr"  # what the file is
    assert diagnostics["analyzable"] is True and document["char_count"] > 0

    tree = client.get(f"{URL}/{document['id']}/structure").json()
    point = next(node for node in tree["nodes"] if node["label"] == "п. 1.1")
    assert point["text_source"] == "ocr" and "пяти рабочих дней" in point["text_preview"]


def test_in_a_mixed_pdf_only_the_pages_without_text_are_recognised(
    client: TestClient, tmp_path: Path
) -> None:
    response = upload(client, mixed_pdf(tmp_path / "mixed.pdf"))
    assert response.status_code == 202, response.text
    document = settled(client, response.json()["id"])
    diagnostics = document["diagnostics"]
    pages = document["page_count"]
    assert diagnostics["text_layer"] == "partial" and diagnostics["empty_pages"] == [pages]
    assert diagnostics["ocr"]["status"] == "done" and diagnostics["ocr"]["pages"] == [pages]
    tree = client.get(f"{URL}/{document['id']}/structure").json()
    sources = {node["text_source"] for node in tree["nodes"] if node["kind"] != "root"}
    assert sources == {"native", "ocr"}
    assert all(
        node["text_source"] == "native" for node in tree["nodes"] if pages not in node["pages"]
    )


def test_a_failure_is_reported_and_can_be_retried(client: TestClient, tesseract: Path) -> None:
    (tesseract / "fail").touch()
    doc_id = upload(client, SYNTHETIC / "scan.pdf").json()["id"]
    failed = settled(client, doc_id)["diagnostics"]
    assert failed["ocr"]["status"] == "failed" and failed["analyzable"] is False
    assert "the page cannot be read" in failed["ocr"]["error"]
    refused = client.post(f"{URL}/{doc_id}/analyses", json={})
    assert refused.json()["error"]["code"] == "document_not_analyzable"

    (tesseract / "fail").unlink()
    retried = client.post(f"{URL}/{doc_id}/ocr")
    assert retried.status_code == 202, retried.text
    assert retried.json()["diagnostics"]["ocr"]["status"] in ("queued", "running")
    done = settled(client, doc_id)["diagnostics"]
    assert done["ocr"]["status"] == "done" and done["ocr"]["error"] is None
    assert done["analyzable"] is True

    again = client.post(f"{URL}/{doc_id}/ocr")  # nothing to retry once recognised
    assert again.status_code == 400 and again.json()["error"]["code"] == "validation_error"


def test_without_the_engine_a_scan_needs_ocr(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    settings = make_settings(data_dir=tmp_path / "data", ocr_enabled=False)
    with TestClient(create_app(settings)) as client:
        response = upload(client, SYNTHETIC / "scan.pdf")
        assert response.status_code == 201
        diagnostics = response.json()["diagnostics"]
        assert diagnostics["text_layer"] == "needs_ocr" and diagnostics["analyzable"] is False
        assert diagnostics["ocr"]["status"] == "unavailable"
        assert "OCR_ENABLED=false" in diagnostics["ocr"]["error"]
        retried = client.post(f"{URL}/{response.json()['id']}/ocr")
        assert retried.status_code == 400 and "OCR недоступен" in retried.json()["error"]["message"]


def test_an_analysis_cannot_start_while_pages_are_recognised(
    client: TestClient, tesseract: Path
) -> None:
    (tesseract / "hold").touch()
    doc_id = upload(client, SYNTHETIC / "scan.pdf").json()["id"]
    until_running(client, doc_id)
    started = client.post(f"{URL}/{doc_id}/analyses", json={})
    assert started.status_code == 409 and started.json()["error"]["code"] == "ocr_in_progress"
    retried = client.post(f"{URL}/{doc_id}/ocr")
    assert retried.status_code == 400  # already on its way
    (tesseract / "hold").unlink()
    assert settled(client, doc_id)["diagnostics"]["analyzable"] is True


def test_a_recognition_cut_off_by_a_stop_resumes_on_the_next_start(
    settings: Settings, tesseract: Path
) -> None:
    (tesseract / "hold").touch()
    with TestClient(create_app(settings)) as client:
        doc_id = upload(client, SYNTHETIC / "scan.pdf").json()["id"]
        until_running(client, doc_id)
    (tesseract / "hold").unlink()
    with TestClient(create_app(settings)) as client:
        document = settled(client, doc_id)
        assert document["diagnostics"]["ocr"]["status"] == "done"


def test_a_metadata_edit_during_recognition_stays(client: TestClient, tesseract: Path) -> None:
    (tesseract / "hold").touch()
    doc_id = upload(client, SYNTHETIC / "scan.pdf").json()["id"]
    until_running(client, doc_id)
    patched = client.patch(f"{URL}/{doc_id}/metadata", json={"fields": {"number": "15"}})
    assert patched.status_code == 200, patched.text
    (tesseract / "hold").unlink()
    document = settled(client, doc_id)
    assert document["diagnostics"]["ocr"]["status"] == "done"
    assert document["metadata"]["number"] == {"value": "15", "citation": None, "source": "user"}


# --- the document screen -----------------------------------------------------------------


def test_the_page_shows_the_progress_and_reloads_when_it_is_over(
    client: TestClient, tesseract: Path
) -> None:
    (tesseract / "hold").touch()
    doc_id = upload(client, SYNTHETIC / "scan.pdf").json()["id"]
    until_running(client, doc_id)
    html = client.get(f"/documents/{doc_id}").text
    assert "Страницы без текста распознаются" in html
    assert "обработано страниц: 0 из 1" in html
    assert f'hx-get="/documents/{doc_id}/ocr"' in html and "Запустить анализ" not in html
    partial = client.get(f"/documents/{doc_id}/ocr")
    assert partial.status_code == 200 and 'id="ocr-status"' in partial.text

    (tesseract / "hold").unlink()
    settled(client, doc_id)
    over = client.get(f"/documents/{doc_id}/ocr")
    assert over.status_code == 204 and over.headers["HX-Refresh"] == "true"
    html = client.get(f"/documents/{doc_id}").text
    assert "выполнен" in html and "распознано страниц: 1" in html
    assert 'title="Текст распознан OCR">OCR</span>' in html and "Запустить анализ" in html


def test_a_failed_recognition_is_retried_from_the_page(client: TestClient, tesseract: Path) -> None:
    (tesseract / "fail").touch()
    doc_id = upload(client, SYNTHETIC / "scan.pdf").json()["id"]
    settled(client, doc_id)
    html = client.get(f"/documents/{doc_id}").text
    assert "Документ нельзя анализировать" in html and "Повторить распознавание" in html

    (tesseract / "fail").unlink()
    response = client.post(f"/documents/{doc_id}/ocr", follow_redirects=False)
    assert response.status_code == 303
    assert settled(client, doc_id)["diagnostics"]["ocr"]["status"] == "done"
