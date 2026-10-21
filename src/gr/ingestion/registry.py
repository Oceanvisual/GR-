"""Which formats have an extractor; everything else is `unsupported_format` (FR-007)."""

from collections.abc import Iterable

from gr.domain.enums import DocFormat
from gr.ingestion.base import Extractor, IngestionError


class ExtractorRegistry:
    def __init__(self, extractors: Iterable[Extractor] = ()) -> None:
        self._by_format: dict[DocFormat, Extractor] = {}
        for extractor in extractors:
            self.register(extractor)

    def register(self, extractor: Extractor) -> None:
        if extractor.format in self._by_format:
            raise ValueError(f"extractor for {extractor.format} already registered")
        self._by_format[extractor.format] = extractor

    def supported(self) -> list[str]:
        return sorted(fmt.value for fmt in self._by_format)

    def get(self, fmt: DocFormat) -> Extractor:
        extractor = self._by_format.get(fmt)
        if extractor is None:
            raise IngestionError(
                "unsupported_format",
                f"Формат {fmt.value.upper()} пока не поддерживается.",
                {"detected": fmt.value, "supported": self.supported()},
            )
        return extractor
