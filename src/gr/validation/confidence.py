"""Confidence from verification results only (R-11, FR-027); the model's own view is not used.

- high: every citation of the element is an exact verified quote;
- medium: all citations passed, but some are fuzzy, relocated or from OCR text, or the
  element is an interpretation;
- low: the main citation passed, but a secondary field (performer, deadline, condition) did
  not; such a field is shown as «не подтверждено» and left out of calculations.
An element whose main citation fails is not a fact at all: it goes to «Не подтверждено».
"""

from collections.abc import Iterable

from gr.domain.enums import Confidence

_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
LOW_SHARE_FOR_LOW_PROCESS = 1 / 3


def lowest(values: Iterable[Confidence]) -> Confidence:
    return min(values, key=_RANK.__getitem__, default=Confidence.MEDIUM)


def element_confidence(
    citations: Iterable[Confidence], *, secondary_failed: bool = False, interpretation: bool = False
) -> Confidence:
    """Confidence of an element from the confidences of its passed citations."""
    if secondary_failed:
        return Confidence.LOW
    value = lowest(citations)
    if interpretation and value == Confidence.HIGH:
        return Confidence.MEDIUM
    return value


def process_confidence(name: Confidence, operations: list[Confidence]) -> Confidence:
    """High when the name and every operation are high; low when there are no operations or
    more than a third of them are low; medium otherwise."""
    if not operations:
        return Confidence.LOW
    low = sum(1 for c in operations if c == Confidence.LOW)
    if low / len(operations) > LOW_SHARE_FOR_LOW_PROCESS:
        return Confidence.LOW
    if name == Confidence.HIGH and all(c == Confidence.HIGH for c in operations):
        return Confidence.HIGH
    return Confidence.MEDIUM
