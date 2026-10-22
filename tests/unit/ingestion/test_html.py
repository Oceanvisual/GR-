"""HTML pages of acts (BR-06, T018)."""

from pathlib import Path

import pytest

from gr.domain.enums import DocFormat
from gr.ingestion.base import IngestionError
from gr.ingestion.html import HtmlExtractor, decode_html
from gr.ingestion.txt import TxtExtractor
from gr.structure.tree import normalized_blocks

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SYNTHETIC = FIXTURES / "synthetic"


def blocks(html: str, encoding: str = "utf-8") -> list[tuple[str | None, str]]:
    raw = HtmlExtractor().extract(html.encode(encoding))
    return [(block.html_tag, block.text) for block in raw.blocks]


def test_the_synthetic_page_gives_the_text_of_the_synthetic_txt() -> None:
    page = HtmlExtractor().extract((SYNTHETIC / "regulation.html").read_bytes())
    text = TxtExtractor().extract((SYNTHETIC / "regulation.txt").read_bytes())
    assert [b.text for b in normalized_blocks(page)] == [b.text for b in normalized_blocks(text)]
    assert page.format == DocFormat.HTML and page.diagnostics.encoding == "windows-1251"
    assert "tracking" not in " ".join(b.text for b in page.blocks)  # the script is gone
    assert "Главная" not in " ".join(b.text for b in page.blocks)  # and the navigation


def test_service_elements_and_hidden_ones_go() -> None:
    found = blocks(
        "<html><head><title>Сайт</title><style>p{}</style></head><body>"
        "<header>Шапка сайта</header><nav>Меню</nav><aside>Новости</aside>"
        "<form><input value='Поиск'><button>Найти</button></form>"
        "<p>1. Текст акта.</p><p hidden>Скрыто</p><div style='display: none'>Тоже</div>"
        "<span aria-hidden='true'>И это</span><noscript>Включите JS</noscript>"
        "<footer>© Портал</footer><script>track()</script></body></html>"
    )
    assert found == [("p", "1. Текст акта.")]


def test_block_elements_are_blocks_with_their_tag_and_inline_ones_are_not() -> None:
    found = blocks(
        "<body><h1>ПРИКАЗ</h1><h2>I. Общие <b>положения</b></h2>"
        "<p>1.1. Срок —&nbsp;десять <a href='#'>рабочих</a>\n   дней.</p>"
        "<ul><li>а) заявление;</li><li>б) паспорт.</li></ul>"
        "<div>2. Текст в div<br>с переносом строки</div></body>"
    )
    assert found == [
        ("h1", "ПРИКАЗ"),
        ("h2", "I. Общие положения"),
        ("p", "1.1. Срок — десять рабочих дней."),
        ("li", "а) заявление;"),
        ("li", "б) паспорт."),
        ("div", "2. Текст в div"),
        ("div", "с переносом строки"),
    ]


def test_a_table_cell_is_one_block_of_its_row() -> None:
    raw = HtmlExtractor().extract(
        "<body><p>Сроки:</p><table><tr><th>Процедура</th><th>Срок</th></tr>"
        "<tr><td><p>Приём</p><p>заявления</p></td><td>1 день</td></tr></table></body>".encode()
    )
    cells = [(b.text, b.table.row, b.table.col) for b in raw.blocks if b.table is not None]
    assert cells == [("Процедура", 0, 0), ("Срок", 0, 1), ("Приём заявления", 1, 0),
                     ("1 день", 1, 1)]  # fmt: skip


def test_a_page_with_main_keeps_only_it() -> None:
    found = blocks("<body><div>Меню сайта</div><main><p>1. Текст акта.</p></main>"
                   "<div>Подвал</div></body>")  # fmt: skip
    assert found == [("p", "1. Текст акта.")]


def test_a_page_with_one_title_starts_at_it() -> None:
    raw = HtmlExtractor().extract((FIXTURES / "corpus" / "mintrud_572n_2020.html").read_bytes())
    assert raw.blocks[0].text == "Приказ Минтруда России № 572н от 7 сентября 2020 г."
    text = " ".join(b.text for b in raw.blocks)
    assert "Утвердить прилагаемый профессиональный стандарт" in text
    assert "Цветовая схема" not in text and "Личный кабинет" not in text  # the site's menus


@pytest.mark.parametrize(
    ("head", "encoding"),
    [
        ('<meta charset="windows-1251">', "windows-1251"),
        ('<meta http-equiv="Content-Type" content="text/html; charset=koi8-r">', "koi8-r"),
    ],
)
def test_the_declared_encoding_is_used(head: str, encoding: str) -> None:
    data = f"<html><head>{head}</head><body><p>Приказ о пропусках</p></body></html>"
    text, found = decode_html(data.encode(encoding))
    assert found == encoding and "Приказ о пропусках" in text


def test_a_wrong_declaration_falls_back_to_detection() -> None:
    data = '<meta charset="utf-8"><p>Административный регламент о выдаче пропусков</p>'
    text, found = decode_html(data.encode("cp1251"))
    assert "Административный регламент" in text and found != "utf-8"


def test_a_page_without_text_is_empty() -> None:
    with pytest.raises(IngestionError) as error:
        HtmlExtractor().extract(b"<html><body><nav>Menu</nav></body></html>")
    assert error.value.code == "empty_document"
