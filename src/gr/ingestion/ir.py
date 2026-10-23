"""Intermediate representation (FR-017). Defined in the domain layer so that structure building,
which may not depend on ingestion, uses the same full-text builder; re-exported here."""

from gr.domain.ir import BLOCK_SEPARATOR, CELL_SEPARATOR, BlockSpan, IRText, full_text

__all__ = ["BLOCK_SEPARATOR", "CELL_SEPARATOR", "BlockSpan", "IRText", "full_text"]
