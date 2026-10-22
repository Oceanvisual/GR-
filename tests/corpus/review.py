"""The structure snapshots as one page a person can check by eye (SC-012, T068).

    uv run python tests/corpus/review.py > docs/structure-review.md

Every document of the regression: its profile and strategy, the counts, and the tree without
paragraphs (they are counted) — each node with its label and the start of its text, indented by
depth. DOC and RTF need LibreOffice; run it in the Docker image to have them all.
"""

import sys
from collections import Counter
from pathlib import Path

from test_structure_regression import CONVERTED, DOCUMENTS, LIMITS

from gr.domain.document import Fragment
from gr.domain.enums import FragmentKind
from gr.ingestion.convert import soffice_binary
from gr.services.parsing import ParsedDocument, parse_file

PREVIEW = 90
HIDDEN = {FragmentKind.PARAGRAPH, FragmentKind.ROOT}


def _depths(fragments: list[Fragment]) -> dict[str, int]:
    depth: dict[str, int] = {}
    for fragment in fragments:
        depth[fragment.id] = depth.get(fragment.parent_id or "", -1) + 1
    return depth


def _line(fragment: Fragment, depth: int) -> str:
    text = " ".join((fragment.heading or fragment.text).split())
    if len(text) > PREVIEW:
        text = text[:PREVIEW] + "…"
    marks = "".join(
        f" _{mark}_"
        for mark, on in (
            ("утратил силу", fragment.repealed),
            ("OCR", fragment.text_source == "ocr"),
        )
        if on
    )
    return f"{'  ' * max(depth - 1, 0)}- **{fragment.label}** — {text}{marks}"


def section(path: Path) -> list[str]:
    title = f"## {path.parent.name}/{path.name}"
    if path.suffix in CONVERTED and soffice_binary() is None:
        return [title, "", "_Не разобран: нужен LibreOffice (запустите в Docker-образе)._", ""]
    parsed = parse_file(str(path), "doc", LIMITS)
    if not isinstance(parsed, ParsedDocument):
        return [title, "", f"_Ошибка разбора: {parsed.code} — {parsed.message}_", ""]
    diagnostics = parsed.diagnostics
    choice = diagnostics.structure
    kinds = Counter(f.kind.value for f in parsed.fragments)
    lines = [
        title,
        "",
        f"- [ ] проверено · профиль `{diagnostics.numbering_profile}` · стратегия "
        f"`{choice.strategy if choice else '—'}` (уверенность "
        f"{choice.confidence if choice else 0:.2f}) · страниц {parsed.page_count or '—'} · "
        f"знаков {len(parsed.text)}",
        "- фрагменты: " + (", ".join(f"{k} {n}" for k, n in sorted(kinds.items())) or "нет"),
        "",
    ]
    depth = _depths(parsed.fragments)
    lines += [_line(f, depth[f.id]) for f in parsed.fragments if f.kind not in HIDDEN]
    return [*lines, ""]


def main() -> int:
    out = [
        "# Снимки структуры: ручная проверка (SC-012)",
        "",
        "Для каждого документа сверьте с самим документом: разделы, главы, статьи и пункты — те "
        "же, с теми же номерами и в том же порядке; утверждаемые документы и приложения — "
        "отдельные корни; ничего лишнего (оглавление, колонтитулы). Абзацы не показаны, только "
        "их число. Проверенный документ отметьте `[x]`.",
        "",
    ]
    for path in DOCUMENTS:
        out += section(path)
    sys.stdout.write("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
