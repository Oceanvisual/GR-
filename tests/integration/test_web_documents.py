"""Document screens: upload form, list, document page, tree, fragment view (T023, ui.md 1–2)."""

import io
import re
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote

import httpx2
import pytest
from fastapi.testclient import TestClient

from gr.app import create_app
from gr.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NPA = FIXTURES / "npa"
SYNTHETIC = FIXTURES / "synthetic"

HOSTILE_NAME = "<img src=x onerror=alert(1)>.txt"
HOSTILE_TEXT = """Порядок <script>alert('title')</script> работы
1. Общие положения <b onclick="alert(2)">жирно</b>
1.1. Заявитель подаёт заявление <script>alert('point')</script> в течение 5 дней.
1.2. Специалист регистрирует заявление.
2. Сроки <iframe src="javascript:alert(3)"></iframe>
2.1. Ответ направляется в течение 10 дней.
"""
RAW_MARKUP = ("<script>alert", "<img src=x", "<b onclick", "<iframe")


def zip_bytes(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


XLSX = zip_bytes({"[Content_Types].xml": "<x/>", "xl/workbook.xml": "<w/>"})


@pytest.fixture
def client(tmp_path: Path, make_settings: Callable[..., Settings]) -> Iterator[TestClient]:
    settings = make_settings(data_dir=tmp_path / "data", ocr_enabled=False, max_upload_mb=1)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def post_file(client: TestClient, name: str, data: bytes, **form: str) -> httpx2.Response:
    return client.post(
        "/documents", files={"file": (name, data)}, data=form, follow_redirects=False
    )


def upload(client: TestClient, path: Path) -> str:
    response = post_file(client, path.name, path.read_bytes())
    assert response.status_code == 303, response.text
    match = re.fullmatch(r"/documents/(doc_\w+)", response.headers["location"])
    assert match, response.headers["location"]
    return match.group(1)


def nodes(client: TestClient, doc_id: str) -> dict[str, str]:
    tree = client.get(f"/api/v1/documents/{doc_id}/structure").json()
    return {node["label"]: node["id"] for node in tree["nodes"]}


# --- screen 1 --------------------------------------------------------------------------------


def test_documents_page_shows_form_limits_and_list(client: TestClient) -> None:
    empty = client.get("/")
    assert empty.status_code == 200
    assert (
        'enctype="multipart/form-data"' in empty.text and 'accept=".docx,.pdf,.txt"' in empty.text
    )
    assert "До 1 МБ, до 300 страниц" in empty.text and "Документов пока нет" in empty.text

    upload(client, NPA / "reg261.docx")
    page = client.get("/").text
    assert "reg261.docx" in page and "текстовый слой есть" in page and "не запускался" in page
    assert re.search(r'href="/documents/doc_\w+"', page)


@pytest.mark.parametrize(
    ("name", "data", "status", "message"),
    [
        ("empty.txt", b"", 422, "Файл пустой."),
        ("table.xlsx", XLSX, 415, "Поддерживаются: DOCX, PDF, TXT."),
        ("broken.pdf", b"%PDF-1.7\n garbage", 422, "PDF не читается"),
        ("big.txt", "а".encode() * (1 << 20), 413, "Файл больше 1 МБ."),
    ],
)
def test_upload_errors_are_shown_in_russian(
    client: TestClient, name: str, data: bytes, status: int, message: str
) -> None:
    response = post_file(client, name, data)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    assert 'role="alert"' in response.text and message in response.text
    assert "<form" in response.text  # the form stays on the page for another try


def test_duplicate_upload_opens_existing_document(client: TestClient) -> None:
    doc_id = upload(client, NPA / "reg261.docx")
    again = post_file(client, "копия.docx", (NPA / "reg261.docx").read_bytes())
    assert again.status_code == 303
    assert again.headers["location"] == f"/documents/{doc_id}?duplicate=1"
    assert "уже загружен раньше" in client.get(again.headers["location"]).text


# --- screen 2 --------------------------------------------------------------------------------


def test_document_page_requisites_diagnostics_tree(client: TestClient) -> None:
    doc_id = upload(client, NPA / "reg261.docx")
    page = client.get(f"/documents/{doc_id}")
    assert page.status_code == 200
    html = page.text
    for text in ("11.09.2020", "261", "30.05.2023", "14.09.2020 № 59820"):
        assert text in html
    # A requisite links to the fragment it was cited from, with the quote's offsets.
    assert re.search(r'hx-get="/fragments/doc_\w+/[^"?]+\?start=\d+&amp;end=\d+"', html)
    assert "нумерация пунктов и статей" in html and "уверенность 1.00" in html
    assert "разделов 6" in html and "пунктов 115" in html
    assert html.count("<details") >= 6 and ">п. 2.16</a>" in html
    assert "Анализ ещё не запускался" in html


def test_scan_page_explains_why_not_analyzable(client: TestClient) -> None:
    doc_id = upload(client, SYNTHETIC / "scan.pdf")
    html = client.get(f"/documents/{doc_id}").text
    assert "Документ нельзя анализировать" in html and "скан: нужен OCR" in html
    assert "Распознавание сканов (OCR) пока не подключено" in html
    assert "Структура пуста" in html


def test_fragment_partial_with_path_neighbours_and_highlight(client: TestClient) -> None:
    doc_id = upload(client, NPA / "reg261.docx")
    fragment_id = nodes(client, doc_id)["п. 3.5.1"]
    plain = client.get(f"/fragments/{doc_id}/{quote(fragment_id)}")
    assert plain.status_code == 200
    html = plain.text
    assert "<html" not in html  # a partial, not a page
    assert "п. 3.5.1" in html and "Специалист" in html and "<mark>" not in html
    assert ">п. 3.5</a>" in html and ">п. 3.5.2</a> →" in html  # breadcrumbs and next

    fragment = client.get(f"/api/v1/documents/{doc_id}/fragments/{fragment_id}").json()
    start = fragment["fragment"]["start"]
    marked = client.get(
        f"/fragments/{doc_id}/{quote(fragment_id)}", params={"start": start, "end": start + 5}
    )
    assert "<mark>3.5.1</mark>" in marked.text


def test_document_page_renders_selected_fragment_without_htmx(client: TestClient) -> None:
    doc_id = upload(client, NPA / "reg261.docx")
    fragment_id = nodes(client, doc_id)["п. 2.6"]
    html = client.get(f"/documents/{doc_id}", params={"fragment": fragment_id}).text
    assert '<article class="fragment-view">' in html and "2.6." in html


def test_unknown_document_and_fragment_are_html_404(client: TestClient) -> None:
    missing = client.get("/documents/doc_00000000000000000000000000")
    assert missing.status_code == 404 and "Документ не найден" in missing.text
    doc_id = upload(client, NPA / "reg261.docx")
    fragment = client.get(f"/fragments/{doc_id}/body/nope")
    assert fragment.status_code == 404 and "Фрагмент не найден" in fragment.text
    selected = client.get(f"/documents/{doc_id}", params={"fragment": "body/nope"})
    assert selected.status_code == 404


def test_edit_metadata_marks_only_changed_fields(client: TestClient) -> None:
    doc_id = upload(client, NPA / "reg261.docx")
    before = client.get(f"/api/v1/documents/{doc_id}").json()["metadata"]
    form = {name: field["value"] or "" for name, field in before.items()}
    form |= {"number": "261-п", "user_source": "  сайт органа  ", "relevance_checked_at": ""}
    response = client.post(f"/documents/{doc_id}/metadata", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/documents/{doc_id}#metadata"

    document = client.get(f"/api/v1/documents/{doc_id}").json()
    after = document["metadata"]
    assert after["number"] == {"value": "261-п", "citation": None, "source": "user"}
    assert after["date"] == before["date"] and after["date"]["citation"]  # untouched
    assert document["user_source"] == "сайт органа"
    assert "введено вручную" in client.get(f"/documents/{doc_id}").text


def test_edit_metadata_rejects_bad_date(client: TestClient) -> None:
    doc_id = upload(client, NPA / "reg261.docx")
    response = client.post(f"/documents/{doc_id}/metadata", data={"relevance_checked_at": "вчера"})
    assert response.status_code == 400 and "ГГГГ-ММ-ДД" in response.text


# --- rendering safety ------------------------------------------------------------------------


def test_document_text_is_never_rendered_as_markup(client: TestClient) -> None:
    response = post_file(client, HOSTILE_NAME, HOSTILE_TEXT.encode())
    assert response.status_code == 303, response.text
    doc_id = response.headers["location"].rsplit("/", 1)[1]
    labels = nodes(client, doc_id)
    pages = [client.get("/").text, client.get(f"/documents/{doc_id}").text]
    pages += [client.get(f"/fragments/{doc_id}/{quote(fid)}").text for fid in labels.values()]
    pages.append(client.get(f"/documents/{doc_id}", params={"fragment": labels["п. 1.1"]}).text)
    for html in pages:
        for markup in RAW_MARKUP:
            assert markup not in html
    assert any("&lt;script&gt;alert(&#39;point&#39;)&lt;/script&gt;" in html for html in pages)
    assert "&lt;img src=x onerror=alert(1)&gt;.txt" in pages[0]

    # A user-entered requisite is escaped too.
    client.post(f"/documents/{doc_id}/metadata", data={"title": "<script>alert('t')</script>"})
    html = client.get(f"/documents/{doc_id}").text
    assert "<script>alert" not in html and "&lt;script&gt;alert(&#39;t&#39;)" in html


def test_odd_query_parameters_still_render_the_page(client: TestClient) -> None:
    doc_id = upload(client, NPA / "reg261.docx")
    fragment_id = nodes(client, doc_id)["п. 2.6"]
    for params in (
        {"start": "-1", "end": "5", "fragment": fragment_id},
        {"start": "abc", "end": "9" * 40, "fragment": fragment_id},
        {"duplicate": "maybe"},
    ):
        page = client.get(f"/documents/{doc_id}", params=params)
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert "<mark>" not in page.text
    partial = client.get(f"/fragments/{doc_id}/{quote(fragment_id)}", params={"start": "x"})
    assert partial.status_code == 200


def test_head_requests(client: TestClient) -> None:
    assert client.head("/").status_code == 200
    assert client.head("/healthz").status_code == 200


def test_metadata_values_are_capped_in_the_api(client: TestClient) -> None:
    doc_id = upload(client, NPA / "reg261.docx")
    url = f"/api/v1/documents/{doc_id}/metadata"
    too_long = client.patch(url, json={"fields": {"number": "x" * 2001}})
    assert too_long.status_code == 400
    assert client.patch(url, json={"fields": {"number": "x" * 2000}}).status_code == 200
