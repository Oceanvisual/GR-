"""IR blocks → fragment tree with stable ids, labels, offsets and pages (FR-010…FR-012, FR-018).

What is not the act's text is taken out first (cleanup.py); several strategies then build the
tree independently and the best-scoring one wins (scoring.py); the editorial notes go back on
the fragments they are about.
"""

from dataclasses import dataclass

from gr.domain.document import Fragment, RawDocument, RemovedNoise, StructureChoice
from gr.domain.enums import StructureMode
from gr.domain.ir import IRText, full_text
from gr.structure.cleanup import clean, place_notes
from gr.structure.scoring import MIN_CONFIDENCE, Choice, choose
from gr.structure.strategies import Strategy
from gr.structure.tree import build_units, flatten, normalized_blocks, stats


class EmptyTextError(ValueError):
    """No text is left after normalisation (invisible characters alone are no text)."""


@dataclass
class StructureResult:
    fragments: list[Fragment]
    text: IRText
    mode: StructureMode
    strategy: str
    warnings: list[str]
    stats: dict[str, int]
    choice: StructureChoice
    removed: RemovedNoise  # service lines and editorial notes taken out of the text


def build_structure(
    raw: RawDocument, doc_id: str, strategies: dict[str, Strategy] | None = None
) -> StructureResult:
    cleaned = clean(normalized_blocks(raw))
    blocks = cleaned.blocks
    ir = full_text(blocks)
    if not ir.text:
        raise EmptyTextError("document has no text")
    units = build_units(blocks, ir)
    chosen: Choice = choose(units, strategies)
    outcome = chosen.outcome
    warnings = list(outcome.warnings)
    if chosen.confidence < MIN_CONFIDENCE and outcome.name != "paragraphs":
        warnings.append(
            f"структура распознана неуверенно (стратегия {outcome.name}, "
            f"уверенность {chosen.confidence:.2f}): проверьте ссылки на пункты"
        )
    fragments = place_notes(flatten(outcome.root, doc_id, ir), ir, cleaned.notes)
    mode = StructureMode.PARAGRAPHS if outcome.name == "paragraphs" else StructureMode.NUMBERED
    return StructureResult(
        fragments=fragments,
        text=ir,
        mode=mode,
        strategy=outcome.name,
        warnings=warnings,
        stats=stats(fragments, ir),
        choice=StructureChoice(
            strategy=outcome.name,
            confidence=chosen.confidence,
            scores=chosen.scores,
            failed_strategies=chosen.failed,
        ),
        removed=RemovedNoise(
            spps_lines=cleaned.spps_lines, editorial_notes=cleaned.editorial_notes
        ),
    )
