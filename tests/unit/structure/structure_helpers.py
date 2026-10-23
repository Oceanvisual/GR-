"""Helpers shared by structure tests: extract any fixture by its extension."""

from pathlib import Path

from gr.domain.document import RawDocument
from gr.ingestion.base import Extractor
from gr.ingestion.docx import DocxExtractor
from gr.ingestion.html import HtmlExtractor
from gr.ingestion.pdf import PdfExtractor
from gr.ingestion.txt import TxtExtractor

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
EXTRACTORS: dict[str, Extractor] = {
    ".txt": TxtExtractor(),
    ".docx": DocxExtractor(),
    ".pdf": PdfExtractor(),
    ".html": HtmlExtractor(),
}


def extract(path: Path) -> RawDocument:
    return EXTRACTORS[path.suffix].extract(path.read_bytes())
