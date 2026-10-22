"""Facts of LLM answers → checked citations (R-11, FR-024, FR-025, FR-027).

A fact names a fragment by alias and quotes it. The quote is verified against that fragment
(or anywhere, if the alias names nothing), and the result says whether the fact may be used.
Quotes from repealed fragments do not count; quotes from OCR text are at most of medium
confidence (FR-008).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from gr.domain.document import Citation, Fragment
from gr.domain.enums import Confidence, GroundingStatus, KnowledgeLayer, TextSource
from gr.domain.process import Grounded
from gr.extraction.render import FragmentAliases
from gr.extraction.schemas import Fact
from gr.grounding.injection import InstructionIndex
from gr.grounding.quotes import QuoteIndex

UNKNOWN_FRAGMENT = "unknown"
INSTRUCTION_REASON = "цитата из фразы, похожей на внедрённую инструкцию для ИИ"
_REASONS = {
    GroundingStatus.PARTIAL: "близкое совпадение с текстом, но не дословное",
    GroundingStatus.UNVERIFIED: "цитата не найдена в тексте акта",
    GroundingStatus.INVALID_QUOTE: "цитата слишком короткая или слишком длинная",
}


@dataclass(frozen=True)
class Checked:
    """One fact after verification."""

    text: str
    citation: Citation
    passed: bool
    confidence: Confidence  # of this citation alone: high, medium or low
    reason: str | None = None  # why it did not pass, for «Не подтверждено»

    @property
    def relocated(self) -> bool:
        return self.citation.grounding.status == GroundingStatus.RELOCATED


class FactMapper:
    def __init__(self, fragments: Sequence[Fragment]) -> None:
        self.aliases = FragmentAliases(fragments)
        self.quotes = QuoteIndex(fragments)
        self.instructions = InstructionIndex(self.quotes.text)
        self.fragments = {f.id: f for f in fragments}

    def resolve(self, ref: str) -> str | None:
        return self.aliases.resolve(ref)

    def check(self, fact: Fact) -> Checked:
        cited = self.resolve(fact.ref)
        # A reference that names no fragment stays the model's claim: found elsewhere, the
        # quote is relocated from it, as from a wrong fragment.
        match = self.quotes.verify(fact.quote, cited or fact.ref.strip() or None)
        grounding = match.grounding
        fragment_id = match.fragment_id or cited or fact.ref.strip() or UNKNOWN_FRAGMENT
        citation = Citation(
            fragment_id=fragment_id, quote=fact.quote.strip() or "—", grounding=grounding
        )
        text = fact.text.strip() or fact.quote.strip()
        if not grounding.status.passes:
            reason = grounding.reason or _REASONS.get(grounding.status, "цитата не подтверждена")
            if cited is None and fact.ref.strip():
                reason = f"фрагмент «{fact.ref.strip()[:20]}» не найден; {reason}"
            return Checked(text, citation, passed=False, confidence=Confidence.LOW, reason=reason)
        fragment = self.fragments.get(match.fragment_id)
        refused = None
        if fragment is not None and fragment.repealed:
            refused = "фрагмент утратил силу"
        elif grounding.matched_start is not None and self.instructions.overlaps(
            grounding.matched_start, grounding.matched_end or grounding.matched_start
        ):
            refused = INSTRUCTION_REASON
        if refused is not None:
            # Found in the text, but not evidence: the status says so everywhere downstream.
            rejected = grounding.model_copy(
                update={"status": GroundingStatus.UNVERIFIED, "reason": refused}
            )
            citation = citation.model_copy(update={"grounding": rejected})
            return Checked(text, citation, passed=False, confidence=Confidence.LOW, reason=refused)
        exact = grounding.status == GroundingStatus.VERIFIED and grounding.score == 100
        confidence = Confidence.HIGH if exact else Confidence.MEDIUM
        if fragment is not None and fragment.text_source == TextSource.OCR:
            confidence = Confidence.MEDIUM  # text recognised by OCR may differ from the act
        return Checked(text, citation, passed=True, confidence=confidence)

    @staticmethod
    def grounded(checked: Checked) -> Grounded[str]:
        """A passed fact as a domain value."""
        assert checked.passed, "only verified facts become Grounded facts"
        return Grounded[str](
            value=checked.text,
            citations=[checked.citation],
            layer=KnowledgeLayer.FACT,
            confidence=checked.confidence,
        )

    @staticmethod
    def interpretation(text: str) -> Grounded[str]:
        """A value the model inferred without a quote (inputs, outputs, systems by name)."""
        return Grounded[str](
            value=text.strip(), layer=KnowledgeLayer.INTERPRETATION, confidence=Confidence.MEDIUM
        )
