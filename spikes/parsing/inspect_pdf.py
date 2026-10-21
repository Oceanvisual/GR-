"""Spike T072: how pypdfium2 delivers a real regulation PDF (lines, headers, indents).

Usage: uv run python -I spikes/parsing/inspect_pdf.py <file.pdf>
"""

import re
import sys
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium

NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s")
SENTENCE_END = re.compile(r"[.;:!?»)]$")


def page_lines(page: pdfium.PdfPage) -> list[tuple[float, float, str]]:
    """Text segments with their left x and top y (PDF points, origin bottom-left)."""
    textpage = page.get_textpage()
    rows = []
    for i in range(textpage.count_rects()):
        left, bottom, right, top = textpage.get_rect(i)
        text = textpage.get_text_bounded(left, bottom, right, top).strip()
        if text:
            rows.append((round(left, 1), round(top, 1), text))
    return rows


def mask(text: str) -> str:
    return re.sub(r"\d+", "#", text)


def main(path: Path) -> None:
    document = pdfium.PdfDocument(str(path))
    pages = [page_lines(page) for page in document]
    raw = document[1].get_textpage().get_text_range()
    print(f"pages: {len(pages)}; raw line separator: {raw[:400].count(chr(13) + chr(10))} CRLF")

    edges = Counter()
    for rows in pages:
        for _, _, text in rows[:2] + rows[-2:]:
            edges[mask(text)] += 1
    repeated = {t: n for t, n in edges.items() if n >= len(pages) * 0.5}
    print(f"repeated edge lines (>=50% pages): {len(repeated)}")
    for text, n in repeated.items():
        print(f"  {n:3} x {text[:100]}")

    body = [r for rows in pages for r in rows if mask(r[2]) not in repeated]
    lefts = Counter(r[0] for r in body)
    print(f"most common left x: {lefts.most_common(5)}")

    numbered = [r for r in body if NUMBERED.match(r[2])]
    indent_of = Counter(r[0] for r in numbered)
    print(f"numbered-looking lines: {len(numbered)}; their left x: {indent_of.most_common(4)}")
    margin = lefts.most_common(1)[0][0]
    at_margin = [r[2][:70] for r in numbered if abs(r[0] - margin) < 2]
    print(
        f"numbered-looking lines at the plain left margin (likely wrapped text): {len(at_margin)}"
    )
    for text in at_margin[:10]:
        print(f"  {text}")

    unterminated = sum(1 for r in body if not SENTENCE_END.search(r[2]))
    hyphen_end = sum(1 for r in body if r[2].endswith("-"))
    print(f"body lines: {len(body)}; no sentence end: {unterminated}; ends with '-': {hyphen_end}")
    print("--- page 3 body lines (x, text):")
    for left, _, text in [r for r in pages[2] if mask(r[2]) not in repeated][:22]:
        print(f"  {left:6} | {text[:95]}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
