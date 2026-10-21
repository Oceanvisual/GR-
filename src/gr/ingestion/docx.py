"""DOCX → IR blocks (R-04, R-05).

Text comes from walking the XML rather than python-docx `paragraph.text`: inserted revisions are
kept, deleted ones dropped, hyperlinks and field results included. Auto-numbering is restored
from numbering.xml. Headers and footers are not read: in exports from legal reference systems
they hold the banner and page numbers (T072). Text boxes and footnotes go to the end, labelled.
"""

import io
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element

from gr.domain.document import Block, IngestionDiagnostics, RawDocument, RemovedNoise, TableCell
from gr.domain.enums import DocFormat, TextLayerStatus
from gr.ingestion.base import ARCHIVE_ERRORS, IngestionError, archive_error
from gr.ingestion.detect import ENCRYPTION_STREAM, OLE_SIGNATURE
from gr.ingestion.docx_numbering import NumberingState, W, parse_numbering

MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_SKIPPED = {f"{W}del", f"{W}moveFrom", f"{W}delText", f"{W}instrText", f"{W}rPr", f"{W}pPr"}
_ALIGN = {"left": "left", "start": "left", "center": "center", "right": "right", "end": "right",
          "both": "justify", "distribute": "justify"}  # fmt: skip

MAX_ENTRIES = 2000
MAX_RATIO = 100


@dataclass(frozen=True)
class ZipLimits:
    max_unzipped_bytes: int = 100 * 1024 * 1024
    max_entries: int = MAX_ENTRIES
    max_ratio: int = MAX_RATIO


def _zip_error(reason: str) -> IngestionError:
    return IngestionError(
        "corrupted_file",
        "Файл повреждён или слишком сильно сжат.",
        {"reason": "zip_limits", "detail": reason},
    )


def check_zip_limits(archive: zipfile.ZipFile, limits: ZipLimits) -> None:
    """Refuse zip bombs by the central directory alone, before inflating anything."""
    entries = archive.infolist()
    if len(entries) > limits.max_entries:
        raise _zip_error(f"{len(entries)} entries")
    total = sum(entry.file_size for entry in entries)
    if total > limits.max_unzipped_bytes:
        raise _zip_error(f"{total} bytes unpacked")
    for entry in entries:
        if (
            entry.file_size > limits.max_ratio * max(entry.compress_size, 1)
            and entry.file_size > 1 << 20
        ):
            raise _zip_error(f"ratio of {entry.filename}")


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except ARCHIVE_ERRORS:
        raise archive_error() from None


def _parse_xml(data: bytes) -> Element:
    head = data[:4096]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data:
        # OOXML never declares entities; refusing them rules out entity-expansion attacks.
        raise IngestionError("corrupted_file", "Файл повреждён: недопустимая разметка XML.")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise IngestionError("corrupted_file", "Файл повреждён: XML не читается.") from None


def _flag(element: Element | None) -> bool:
    if element is None:
        return False
    return element.get(f"{W}val", "true") not in ("0", "false", "off")


@dataclass(frozen=True)
class _Style:
    name: str
    based_on: str | None
    num_id: str | None
    ilvl: int | None
    bold: bool | None
    align: str | None
    size: float | None


def _parse_styles(root: Element | None) -> dict[str, _Style]:
    styles: dict[str, _Style] = {}
    if root is None:
        return styles
    for style in root.iter(f"{W}style"):
        style_id = style.get(f"{W}styleId") or ""
        name = style.find(f"{W}name")
        ppr, rpr = style.find(f"{W}pPr"), style.find(f"{W}rPr")
        num_pr = ppr.find(f"{W}numPr") if ppr is not None else None
        ilvl = num_pr.find(f"{W}ilvl") if num_pr is not None else None
        size = rpr.find(f"{W}sz") if rpr is not None else None
        bold = rpr.find(f"{W}b") if rpr is not None else None
        jc = ppr.find(f"{W}jc") if ppr is not None else None
        based_on = style.find(f"{W}basedOn")
        styles[style_id] = _Style(
            name=(name.get(f"{W}val") if name is not None else None) or style_id,
            based_on=based_on.get(f"{W}val") if based_on is not None else None,
            num_id=(num_pr.find(f"{W}numId").get(f"{W}val")  # type: ignore[union-attr]
                    if num_pr is not None and num_pr.find(f"{W}numId") is not None else None),
            ilvl=int(ilvl.get(f"{W}val") or 0) if ilvl is not None else None,
            bold=_flag(bold) if bold is not None else None,
            align=jc.get(f"{W}val") if jc is not None else None,
            size=float(size.get(f"{W}val") or 0) / 2 if size is not None else None,
        )  # fmt: skip
    return styles


def _inherited[T](styles: dict[str, _Style], style_id: str | None, attr: str) -> T | None:
    seen: set[str] = set()
    while style_id and style_id in styles and style_id not in seen:
        seen.add(style_id)
        value: T | None = getattr(styles[style_id], attr)
        if value is not None:
            return value
        style_id = styles[style_id].based_on
    return None


@dataclass
class _Paragraph:
    parts: list[str] = field(default_factory=list)
    bold_chars: int = 0
    chars: int = 0
    size: float | None = None


class _Walker:
    def __init__(self, styles: dict[str, _Style], numbering: NumberingState) -> None:
        self.styles = styles
        self.numbering = numbering
        self.blocks: list[Block] = []
        self.sidebars: list[str] = []
        self.table_index = 0
        self.restored = False

    # --- inline text ---------------------------------------------------------------------

    def _inline(self, element: Element, out: _Paragraph, run_bold: bool) -> None:
        for child in element:
            tag = child.tag
            if tag in _SKIPPED:
                continue
            if tag == f"{MC}AlternateContent":
                choice = child.find(f"{MC}Choice")  # Choice and Fallback hold the same content
                if choice is not None:
                    self._inline(choice, out, run_bold)
                continue
            if tag == f"{W}txbxContent":
                text = " ".join(t for p in child.iter(f"{W}p") if (t := self.paragraph_text(p)))
                if text.strip():
                    self.sidebars.append(text.strip())
                continue
            if tag == f"{W}r":
                rpr = child.find(f"{W}rPr")
                bold = run_bold
                if rpr is not None and rpr.find(f"{W}b") is not None:
                    bold = _flag(rpr.find(f"{W}b"))
                if rpr is not None and out.size is None and rpr.find(f"{W}sz") is not None:
                    out.size = float(rpr.find(f"{W}sz").get(f"{W}val") or 0) / 2  # type: ignore[union-attr]
                before = sum(len(p) for p in out.parts)
                self._inline(child, out, bold)
                added = sum(len(p) for p in out.parts) - before
                visible = added  # whitespace-only runs still count; harmless for the ratio
                out.chars += visible
                out.bold_chars += visible if bold else 0
                continue
            if tag == f"{W}t":
                out.parts.append(child.text or "")
            elif tag in (f"{W}tab", f"{W}ptab"):
                out.parts.append("\t")
            elif tag in (f"{W}br", f"{W}cr"):
                out.parts.append(" ")
            elif tag == f"{W}noBreakHyphen":
                out.parts.append("-")
            elif tag == f"{W}softHyphen":
                out.parts.append("­")
            else:
                self._inline(child, out, run_bold)

    def paragraph_text(self, p: Element) -> str:
        out = _Paragraph()
        self._inline(p, out, False)
        return "".join(out.parts)

    # --- block level ---------------------------------------------------------------------

    def body(self, container: Element) -> None:
        for child in container:
            if child.tag == f"{W}p":
                self.paragraph(child)
            elif child.tag == f"{W}tbl":
                self.table(child)
                self.table_index += 1
            elif child.tag == f"{W}sdt":
                content = child.find(f"{W}sdtContent")
                if content is not None:
                    self.body(content)

    def paragraph(self, p: Element) -> None:
        ppr = p.find(f"{W}pPr")
        style_id = None
        num_id: str | None = None
        ilvl: int | None = None
        jc = None
        indent = None
        if ppr is not None:
            pstyle = ppr.find(f"{W}pStyle")
            style_id = pstyle.get(f"{W}val") if pstyle is not None else None
            num_pr = ppr.find(f"{W}numPr")
            if num_pr is not None:
                num_el, ilvl_el = num_pr.find(f"{W}numId"), num_pr.find(f"{W}ilvl")
                num_id = num_el.get(f"{W}val") if num_el is not None else None
                ilvl = int(ilvl_el.get(f"{W}val") or 0) if ilvl_el is not None else None
            jc_el = ppr.find(f"{W}jc")
            jc = jc_el.get(f"{W}val") if jc_el is not None else None
            ind = ppr.find(f"{W}ind")
            if ind is not None and ind.get(f"{W}firstLine"):
                indent = float(ind.get(f"{W}firstLine") or 0) / 20  # twips → points
        num_id = num_id or _inherited(self.styles, style_id, "num_id")
        if ilvl is None:
            ilvl = _inherited(self.styles, style_id, "ilvl") or 0

        style_bold = bool(_inherited(self.styles, style_id, "bold"))
        out = _Paragraph()
        self._inline(p, out, style_bold)
        text = "".join(out.parts).strip()

        # A numbered paragraph consumes its number even when empty, as in Word.
        marker = self.numbering.next_marker(num_id, ilvl) if num_id else None
        if not text:
            return
        if marker:
            self.restored = True
            text = f"{marker} {text}"
        align = jc or _inherited(self.styles, style_id, "align")
        self.blocks.append(
            Block(
                text=text,
                style=self.styles[style_id].name if style_id in self.styles else style_id,
                bold=out.chars > 0 and out.bold_chars >= 0.9 * out.chars,
                align=_ALIGN.get(align or ""),  # type: ignore[arg-type]
                indent=indent,
                font_size=out.size or _inherited(self.styles, style_id, "size") or None,
                list_level=ilvl if marker else None,
                list_marker=marker,
            )
        )

    def _cell_text(self, cell: Element) -> str:
        texts: list[str] = []
        for child in cell:
            if child.tag == f"{W}p":
                texts.append(self.paragraph_text(child).strip())
            elif child.tag == f"{W}tbl":
                rows = [
                    " | ".join(t for tc in tr.findall(f"{W}tc") if (t := self._cell_text(tc)))
                    for tr in child.findall(f"{W}tr")
                ]
                texts.append(" ; ".join(r for r in rows if r))
            elif child.tag == f"{W}sdt":
                content = child.find(f"{W}sdtContent")
                if content is not None:
                    texts.append(self._cell_text(content))
        return " ".join(t for t in texts if t)

    def table(self, tbl: Element) -> None:
        for row_index, tr in enumerate(tbl.findall(f"{W}tr")):
            for col_index, tc in enumerate(tr.findall(f"{W}tc")):
                text = self._cell_text(tc).strip()
                if text:
                    cell = TableCell(table=self.table_index, row=row_index, col=col_index)
                    self.blocks.append(Block(text=text, table=cell))


def _notes(archive: zipfile.ZipFile, name: str, label: str, walker: _Walker) -> list[Block]:
    if name not in archive.namelist():
        return []
    root = _parse_xml(_read(archive, name))
    blocks: list[Block] = []
    for note in root:
        if note.get(f"{W}type") in ("separator", "continuationSeparator", "continuationNotice"):
            continue
        text = " ".join(t for p in note.iter(f"{W}p") if (t := walker.paragraph_text(p).strip()))
        if text:
            blocks.append(Block(text=f"[{label} {note.get(f'{W}id')}] {text}"))
    return blocks


def _header_footer_lines(archive: zipfile.ZipFile, walker: _Walker) -> int:
    count = 0
    for name in archive.namelist():
        if name.startswith(("word/header", "word/footer")) and name.endswith(".xml"):
            root = _parse_xml(_read(archive, name))
            count += sum(1 for p in root.iter(f"{W}p") if walker.paragraph_text(p).strip())
    return count


def _optional_part(archive: zipfile.ZipFile, names: set[str], name: str) -> Element | None:
    return _parse_xml(_read(archive, name)) if name in names else None


class DocxExtractor:
    format = DocFormat.DOCX

    def __init__(self, limits: ZipLimits | None = None) -> None:
        self.limits = limits or ZipLimits()

    def extract(self, data: bytes) -> RawDocument:
        if data.startswith(OLE_SIGNATURE):
            if ENCRYPTION_STREAM in data:
                raise IngestionError("encrypted_file", "Файл защищён паролем.")
            raise IngestionError("corrupted_file", "Файл повреждён: это не DOCX.")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except ARCHIVE_ERRORS:
            raise archive_error() from None
        with archive:
            check_zip_limits(archive, self.limits)
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                raise IngestionError("corrupted_file", "Файл повреждён: нет текста документа.")
            document = _parse_xml(_read(archive, "word/document.xml"))
            styles = _parse_styles(_optional_part(archive, names, "word/styles.xml"))
            numbering = NumberingState(
                parse_numbering(_optional_part(archive, names, "word/numbering.xml"))
            )

            walker = _Walker(styles, numbering)
            body = document.find(f"{W}body")
            if body is not None:
                walker.body(body)
            blocks = list(walker.blocks)
            blocks += [Block(text=f"[Надпись] {text}") for text in walker.sidebars]
            blocks += _notes(archive, "word/footnotes.xml", "Сноска", walker)
            blocks += _notes(archive, "word/endnotes.xml", "Концевая сноска", walker)
            header_footer = _header_footer_lines(archive, walker)

        if not any(block.text.strip() for block in blocks):
            raise IngestionError("empty_document", "В документе нет текста.")
        return RawDocument(
            format=DocFormat.DOCX,
            blocks=blocks,
            diagnostics=IngestionDiagnostics(
                text_layer=TextLayerStatus.OK,
                numbering_restored=walker.restored,
                warnings=sorted(numbering.warnings),
                removed_noise=RemovedNoise(header_footer_lines=header_footer),
            ),
        )
