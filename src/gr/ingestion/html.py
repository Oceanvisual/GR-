"""HTML pages of acts (BR-06, T018): the text of the page as IR blocks.

The encoding is the one the page declares (`<meta charset>` or `http-equiv`), checked by
decoding; without a declaration, or with a wrong one, it is detected as for plain text. What is
not the act is dropped: scripts, styles, navigation, page headers and footers, forms and hidden
elements. A page with a `<main>` (or one `<article>`) keeps only that part; a page with one
`<h1>` starts there, the site's own menus above it are not the act. Every block element
(paragraph, heading, list item, `<div>` with text) becomes a block carrying its tag, a line
break ends a block, and a table cell is one block of its row, as in DOCX.
"""

import codecs
import re
from collections.abc import Iterator

from selectolax.lexbor import LexborHTMLParser, LexborNode

from gr.domain.document import Block, IngestionDiagnostics, RawDocument, TableCell
from gr.domain.enums import DocFormat, TextLayerStatus
from gr.ingestion.base import IngestionError
from gr.ingestion.txt import decode_text

_DECLARED_CHARSET_RE = re.compile(rb"<meta\b[^>]*?charset\s*=\s*[\"']?\s*([\w.:-]+)", re.I)
_CHARSET_PROBE = 4096
_DROPPED = frozenset(
    {
        "script", "style", "noscript", "template", "head", "nav", "header", "footer", "aside",
        "form", "button", "select", "textarea", "input", "svg", "iframe", "object", "canvas",
    }
)  # fmt: skip
_BLOCKS = frozenset(
    {
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dd", "dt", "blockquote", "pre",
        "section", "article", "main", "body", "ul", "ol", "dl", "caption", "address", "center",
        "figure", "figcaption", "hr",
    }
)  # fmt: skip
_CELLS = ("td", "th")
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def decode_html(data: bytes) -> tuple[str, str]:
    """Bytes → (text, encoding): the declared charset if it decodes, otherwise detected."""
    if not data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        declared = _DECLARED_CHARSET_RE.search(data[:_CHARSET_PROBE])
        if declared is not None:
            name = declared.group(1).decode("ascii", "replace").lower()
            try:
                return data.decode(codecs.lookup(name).name), name
            except (LookupError, UnicodeDecodeError):
                pass  # an unknown or a wrong declaration: detect as plain text does
    return decode_text(data)


def _hidden(node: LexborNode) -> bool:
    attributes = node.attributes
    return (
        "hidden" in attributes
        or (attributes.get("aria-hidden") or "").lower() == "true"
        or bool(_HIDDEN_STYLE_RE.search(attributes.get("style") or ""))
    )


def _collapse(text: str) -> str:
    return " ".join(text.split())


class _Walker:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.pending: list[str] = []
        self.tables = 0

    def flush(self, tag: str) -> None:
        text = _collapse("".join(self.pending))
        self.pending.clear()
        if text:
            self.blocks.append(Block(text=text, html_tag=tag))

    def walk(self, node: LexborNode, tag: str) -> None:
        """The text of `node`, whose nearest block element is `tag`."""
        for child in node.iter(include_text=True):
            if child.is_text_node:
                self.pending.append(child.text_content or "")
                continue
            if not child.is_element_node or child.tag in _DROPPED or _hidden(child):
                continue
            name = child.tag or ""
            if name == "br":
                self.flush(tag)
            elif name == "table":
                self.flush(tag)
                self.table(child)
            elif name in _BLOCKS or name in _CELLS or name == "tr":
                self.flush(tag)
                self.walk(child, name)
                self.flush(name)
            else:
                self.walk(child, tag)  # inline: a, span, b, sup…

    def table(self, table: LexborNode) -> None:
        number = self.tables
        self.tables += 1
        for row_index, row in enumerate(_rows(table)):
            for col_index, cell in enumerate(c for c in row.iter() if c.tag in _CELLS):
                if _hidden(cell):
                    continue
                inner = _Walker()
                inner.tables = self.tables
                inner.walk(cell, cell.tag or "td")
                inner.flush(cell.tag or "td")
                self.tables = inner.tables
                text = " ".join(block.text for block in inner.blocks)  # one block a cell
                if text:
                    position = TableCell(table=number, row=row_index, col=col_index)
                    self.blocks.append(Block(text=text, table=position, html_tag=cell.tag))


def _rows(table: LexborNode) -> Iterator[LexborNode]:
    """The rows of a table, not of the tables nested in its cells."""
    for child in table.iter():
        if child.tag == "tr":
            yield child
        elif child.tag in ("thead", "tbody", "tfoot"):
            yield from (row for row in child.iter() if row.tag == "tr")


def _content(tree: LexborHTMLParser) -> LexborNode | None:
    """`<main>`, or the only `<article>`, when the page marks its content; else the body."""
    for selector in ("main", "article"):
        found = [node for node in tree.css(selector) if _collapse(node.text(separator=" "))]
        if len(found) == 1:
            return found[0]
    return tree.body


def _from_title(blocks: list[Block]) -> list[Block]:
    titles = [i for i, block in enumerate(blocks) if block.html_tag == "h1"]
    return blocks[titles[0] :] if len(titles) == 1 else blocks


class HtmlExtractor:
    format = DocFormat.HTML

    def extract(self, data: bytes) -> RawDocument:
        text, encoding = decode_html(data)
        tree = LexborHTMLParser(text)
        content = _content(tree)
        walker = _Walker()
        if content is not None:
            walker.walk(content, content.tag or "body")
            walker.flush(content.tag or "body")
        blocks = _from_title(walker.blocks)
        if not blocks:
            raise IngestionError("empty_document", "В документе нет текста.")
        return RawDocument(
            format=DocFormat.HTML,
            blocks=blocks,
            diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK, encoding=encoding),
        )
