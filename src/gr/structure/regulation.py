"""Strategy `regulation`: numbering of Russian legal drafting (R-06).

Sections, chapters, articles with parts, unnumbered subsection headings, decimal points, subpoints
«1)», «3.1)», «а)», dash lists and paragraphs; preamble and approved documents; appendices.
"""

from dataclasses import dataclass, field

from gr.domain.enums import FragmentKind
from gr.structure.numbering import (
    Verdict,
    parse_number,
    plausible_letter,
    plausible_point,
    plausible_subpoint,
)
from gr.structure.patterns import (
    Marker,
    Match,
    classify,
    looks_like_heading,
    roman_to_int,
    starts_lowercase,
)
from gr.structure.tree import Node, Unit, paragraph_node, short_title

MAX_HEADING_LINES = 12  # headings broken into lines by hand
MAX_CENTRED_HEADING_LINES = 30  # while lines stay centred like the first one
_DATE_OR_NUMBER = ("от ", " N ", "№")
_K = FragmentKind


@dataclass
class RegulationOutcome:
    root: Node
    warnings: list[str] = field(default_factory=list)
    numbered: int = 0  # accepted structural markers
    rejected: int = 0  # number-like paragraphs that did not continue any numbering
    duplicates: int = 0


class RegulationBuilder:
    """Sections, chapters, unnumbered subsection headings, decimal points, subpoints, lists."""

    def __init__(self, units: list[Unit]) -> None:
        self.units = units
        self.root = Node(_K.ROOT, "", "документ", ref="")
        self.warnings: list[str] = []
        self.matches = [classify(unit.text) for unit in units]
        self.structured = 0
        self.rejected = 0
        self.duplicates = 0
        self.container: Node = self.root  # where points of the current context attach
        self.scope_root: Node = self.root  # preamble, approved document or appendix
        self.points: list[Node] = []  # open decimal points, outermost first
        self.subs: list[Node] = []  # open subpoints, outermost first
        self.last_point: tuple[int, ...] | None = None
        self.section_number: int | None = None
        self.section_changed = False
        self.approved_count = 0
        self.headings_in: dict[int, int] = {}

    # --- helpers -------------------------------------------------------------------------

    def _deepest(self) -> Node:
        if self.subs:
            return self.subs[-1]
        if self.points:
            return self.points[-1]
        return self.container

    def _paragraph(self, parent: Node, unit: Unit, kind: FragmentKind = _K.PARAGRAPH) -> None:
        paragraph_node(parent, unit, kind)

    def _reset_items(self) -> None:
        self.points.clear()
        self.subs.clear()

    def _open_scope(self, node: Node, scope: str) -> None:
        node.scope = scope
        self.scope_root = node
        self.container = node
        self._reset_items()
        self.last_point = None
        self.section_number = None
        self.section_changed = False

    # --- main loop -----------------------------------------------------------------------

    def build(self) -> Node:
        openers = (Marker.APPROVED, Marker.ACT_APPENDIX)
        first_approved = next((i for i, m in enumerate(self.matches) if m.marker in openers), None)
        i = 0
        if first_approved is not None and first_approved > 0:
            preamble = self.root.add(
                Node(_K.PREAMBLE, "preamble", "вводная часть", ref="вводной части")
            )
            self._open_scope(preamble, " вводной части")
            while i < first_approved:
                self._item(i)
                i += 1
        while i < len(self.units):
            i = self._step(i)
        return self.root

    def _step(self, i: int) -> int:
        match = self.matches[i]
        unit = self.units[i]
        if match.marker in (Marker.APPROVED, Marker.ACT_APPENDIX):
            return self._approved(i)
        if match.marker == Marker.ARTICLE and not unit.in_table:
            return self._article(i, match)
        if match.marker == Marker.APPENDIX and not unit.in_table:
            return self._appendix(i)
        if match.marker in (Marker.SECTION, Marker.CHAPTER) and not unit.in_table:
            return self._section(i, match)
        if self._is_heading(i):
            return self._subsection(i)
        self._item(i)
        return i + 1

    def _continuation(self, i: int, *, lowercase_only: bool, limit: int = 6) -> int:
        """Index after the heading lines that continue unit i."""
        j = i + 1
        while j < len(self.units) and j - i <= limit:
            unit, match = self.units[j], self.matches[j]
            if match.marker != Marker.TEXT or unit.in_table:
                break
            if starts_lowercase(unit.text) and len(unit.text) <= 200:
                j += 1
                continue
            if _upper(self.units[i].text) and _upper(unit.text) and len(unit.text) <= 200:
                j += 1  # a heading in capitals carried over to the next line
                continue
            if not lowercase_only and looks_like_heading(unit.text):
                j += 1
                continue
            break
        return j

    def _approved(self, i: int) -> int:
        self.approved_count += 1
        segment = "body" if self.approved_count == 1 else f"body{self.approved_count}"
        node = self.root.add(Node(_K.APPROVED_DOCUMENT, segment, "утверждаемый документ", ref=""))
        self._open_scope(node, "")
        end = i + 1
        approved_seen = self.matches[i].marker == Marker.APPROVED
        while end < len(self.units) and end - i <= 12:
            unit, match = self.units[end], self.matches[end]
            if match.marker == Marker.APPROVED and not approved_seen:
                approved_seen = True  # «Приложение N к приказу» + «УТВЕРЖДЕН»: one header
                end += 1
                continue
            if match.marker != Marker.TEXT or unit.in_table or len(unit.text) > 300:
                break
            if unit.text.rstrip().endswith((".", ";", ":")) and not any(
                s in unit.text for s in _DATE_OR_NUMBER
            ):
                break
            end += 1
        node.units = self.units[i:end]
        reference = max(
            (k for k, u in enumerate(node.units) if any(s in u.text for s in _DATE_OR_NUMBER)),
            default=0,
        )
        title_units = [
            u for u in node.units[reference + 1 :] or node.units[1:] if not _approves(u.text)
        ]
        node.heading = " ".join(u.text for u in title_units) or None
        self.structured += 1
        return end

    def _appendix(self, i: int) -> int:
        number = self.matches[i].number
        parent = self.scope_root if self.scope_root.kind == _K.APPROVED_DOCUMENT else self.root
        if self.scope_root.kind == _K.APPENDIX:
            parent = self.scope_root.parent or self.root
        label = f"прил. {number}" if number else "прил."
        node = parent.add(Node(_K.APPENDIX, f"app{number or ''}", label, number=number))
        end = self._continuation(i, lowercase_only=False, limit=4)
        node.units = self.units[i:end]
        node.heading = " ".join(u.text for u in node.units)
        self._open_scope(node, f" {label}")
        self.structured += 1
        return end

    def _section(self, i: int, match: Match) -> int:
        number = match.number or ""
        is_chapter = match.marker == Marker.CHAPTER
        parent = self.scope_root
        if is_chapter and self.container.kind == _K.SECTION:
            parent = self.container
        elif is_chapter and self.container.kind == _K.CHAPTER and self.container.parent:
            parent = self.container.parent
        kind = _K.CHAPTER if is_chapter else _K.SECTION
        label = f"гл. {number}" if is_chapter else f"разд. {number}"
        node = parent.add(
            Node(
                kind,
                ("ch" if is_chapter else "") + number,
                label + self.scope_root.scope,
                number=number,
            )
        )
        end = self._continuation(i, lowercase_only=bool(match.title))
        node.units = self.units[i:end]
        node.heading = " ".join(u.text for u in node.units)
        self.container = node
        self._reset_items()
        if not is_chapter:
            value = int(number) if number.isdigit() else roman_to_int(number)
            self.section_changed = value != self.section_number
            self.section_number = value
        self.structured += 1
        return end

    def _article(self, i: int, match: Match) -> int:
        number = match.number or ""
        parent = self.container
        while parent.kind in (_K.SUBSECTION, _K.ARTICLE) and parent.parent is not None:
            parent = parent.parent
        label = f"ст. {number}"
        node = parent.add(Node(_K.ARTICLE, f"art{number}", label, number=number))
        end = self._continuation(i, lowercase_only=bool(match.title))
        node.units = self.units[i:end]
        node.heading = " ".join(u.text for u in node.units)
        self.container = node
        self._reset_items()
        self.last_point = None  # parts restart in every article
        self.structured += 1
        return end

    def _is_heading(self, i: int) -> bool:
        unit = self.units[i]
        if unit.in_table or self.matches[i].marker != Marker.TEXT:
            return False
        if self.scope_root.kind == _K.PREAMBLE:
            return False
        styled = unit.centered or unit.emphasized
        nxt_lower = i + 1 < len(self.units) and starts_lowercase(self.units[i + 1].text)
        if styled and unit.text.rstrip().endswith(",") and nxt_lower:
            return len(unit.text) <= 200  # a styled heading whose first line ends with a comma
        if not looks_like_heading(unit.text):
            return False
        if styled:
            return True
        # Without styling, a short line is a heading only if numbered structure follows it:
        # otherwise a stray short line (a note, a signature) would close the current point.
        previous = self.units[i - 1].text.rstrip() if i > 0 else ""
        follows_break = not previous or previous.endswith((".", ";", ":"))
        j = i + 1
        while j < len(self.units) and j - i <= 3 and self._heading_line(j):
            j += 1
        structural = (Marker.POINT, Marker.SECTION, Marker.CHAPTER, Marker.APPENDIX)
        return follows_break and j < len(self.units) and self.matches[j].marker in structural

    def _subsection(self, i: int) -> int:
        parent = self.container
        while parent.kind == _K.SUBSECTION and parent.parent is not None:
            parent = parent.parent
        count = self.headings_in.get(id(parent), 0) + 1
        self.headings_in[id(parent)] = count
        end = i + 1
        centred = self.units[i].centered
        while end < len(self.units) and self._heading_line(end):
            limit = (
                MAX_CENTRED_HEADING_LINES
                if centred and self.units[end].centered
                else MAX_HEADING_LINES
            )
            if end - i >= limit:
                break
            end += 1
        heading = " ".join(u.text for u in self.units[i:end])
        label = f"подразд. «{short_title(heading)}»"
        node = parent.add(Node(_K.SUBSECTION, f"h{count}", label, heading=heading))
        node.units = self.units[i:end]
        node.scope = parent.scope
        self.container = node
        self._reset_items()
        return end

    def _heading_line(self, j: int) -> bool:
        unit = self.units[j]
        return (
            not unit.in_table
            and self.matches[j].marker == Marker.TEXT
            and (looks_like_heading(unit.text) or starts_lowercase(unit.text))
            and len(unit.text) <= 200
            and not unit.text.rstrip().endswith((".", ";", ":"))
        )

    # --- items: points, subpoints, list items, paragraphs ---------------------------------

    def _item(self, i: int) -> None:
        unit, match = self.units[i], self.matches[i]
        if not unit.in_table:
            if match.marker == Marker.POINT and match.number and self._point(unit, match.number):
                return
            if (
                match.marker == Marker.SUBPOINT
                and match.number
                and self._subpoint(unit, match.number)
            ):
                return
            if match.marker == Marker.LETTER and match.number and self._letter(unit, match.number):
                return
            if match.marker == Marker.LIST_ITEM:
                self._paragraph(self._deepest(), unit, _K.LIST_ITEM)
                return
        if match.marker in (Marker.POINT, Marker.SUBPOINT, Marker.LETTER) and not unit.in_table:
            self.rejected += 1
        # Plain text belongs to the point, not to its last subpoint.
        self.subs.clear()
        self._paragraph(self._deepest(), unit)

    def _point(self, unit: Unit, number: str) -> bool:
        candidate = parse_number(number)
        verdict = plausible_point(
            self.last_point,
            candidate,
            section=self.section_number,
            section_changed=self.section_changed,
        )
        if verdict == Verdict.REJECT:
            return False
        if verdict == Verdict.DUPLICATE:
            self.warnings.append(f"повтор номера пункта {number}{self.scope_root.scope}")
            self.duplicates += 1
        while self.points and not _is_prefix(parse_number(self.points[-1].number or ""), candidate):
            self.points.pop()
        self.subs.clear()
        parent = self.points[-1] if self.points else self.container
        if self.container.kind == _K.ARTICLE:
            kind, label = _K.PART, f"ч. {number} {self.container.label}"
        else:
            kind, label = _K.POINT, f"п. {number}{self.scope_root.scope}"
        node = parent.add(Node(kind, number, label, number=number, units=[unit]))
        self.points.append(node)
        self.last_point = candidate
        self.section_changed = False
        self.structured += 1
        return True

    def _item_owner(self) -> Node:
        return self.points[-1] if self.points else self.container

    def _subpoint(self, unit: Unit, number: str) -> bool:
        """«1)»: a child of the current point; closes any open subpoints."""
        owner = self._item_owner()
        candidate = parse_number(number)
        verdict = plausible_subpoint(owner.last_sub, candidate)
        if verdict == Verdict.REJECT:
            return False
        if verdict == Verdict.DUPLICATE:
            self.warnings.append(f"повтор номера подпункта {number}) в {owner.label}")
            self.duplicates += 1
        owner.last_sub = candidate
        self.subs = [self._sub_node(owner, unit, number)]
        return True

    def _letter(self, unit: Unit, letter: str) -> bool:
        """«а)»: nested in an open numbered subpoint if it continues there, else a point's child."""
        owners = [self.subs[0]] if self.subs and _numeric(self.subs[0]) else []
        owners.append(self._item_owner())
        for owner in owners:
            verdict = plausible_letter(owner.last_letter, letter)
            if verdict == Verdict.REJECT:
                continue
            if verdict == Verdict.DUPLICATE:
                self.warnings.append(f"повтор подпункта {letter}) в {owner.label}")
                self.duplicates += 1
            owner.last_letter = letter
            node = self._sub_node(owner, unit, letter)
            self.subs = [owner, node] if owner.kind == _K.SUBPOINT else [node]
            return True
        return False

    def _sub_node(self, owner: Node, unit: Unit, number: str) -> Node:
        # In laws the items of a part are «пункты»: «п. 1 ч. 2 ст. 5».
        prefix = "п." if owner.kind in (_K.PART, _K.ARTICLE) else "пп."
        label = f"{prefix} {number}"
        if owner.reference:
            label = f"{label} {owner.reference}"
        self.structured += 1
        return owner.add(Node(_K.SUBPOINT, f"sp{number}", label, number=number, units=[unit]))


def _upper(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return len(letters) >= 3 and sum(ch.isupper() for ch in letters) >= 0.9 * len(letters)


def _approves(text: str) -> bool:
    return classify(text).marker == Marker.APPROVED


def _numeric(node: Node) -> bool:
    return node.kind == _K.SUBPOINT and (node.number or "")[:1].isdigit()


def _is_prefix(prefix: tuple[int, ...], number: tuple[int, ...]) -> bool:
    return len(prefix) < len(number) and number[: len(prefix)] == prefix


def label_multiple_documents(root: Node) -> None:
    approved = [child for child in root.children if child.kind == _K.APPROVED_DOCUMENT]
    if len(approved) < 2:
        return
    for node in approved:
        short = " ".join((node.heading or node.segment).split()[:3])

        def mark(current: Node, short: str = short) -> None:
            current.approved_doc = short
            current.label = f"{current.label} ({short})"
            for child in current.children:
                mark(child)

        mark(node)


def run_regulation(units: list[Unit]) -> RegulationOutcome:
    builder = RegulationBuilder(units)
    root = builder.build()
    label_multiple_documents(root)
    return RegulationOutcome(
        root=root,
        warnings=builder.warnings,
        numbered=builder.structured,
        rejected=builder.rejected,
        duplicates=builder.duplicates,
    )
