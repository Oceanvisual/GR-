"""Deterministic confidence (R-11, FR-027, T031)."""

import pytest

from gr.domain.enums import Confidence
from gr.validation.confidence import element_confidence, lowest, process_confidence

H, M, L = Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW


@pytest.mark.parametrize(
    ("citations", "secondary_failed", "interpretation", "expected"),
    [
        ([H, H], False, False, H),
        ([H, M], False, False, M),
        ([H], True, False, L),  # a secondary field failed
        ([H], False, True, M),  # an interpretation is at most medium
        ([], False, False, M),
    ],
)
def test_element_confidence(
    citations: list[Confidence], secondary_failed: bool, interpretation: bool, expected: Confidence
) -> None:
    result = element_confidence(
        citations, secondary_failed=secondary_failed, interpretation=interpretation
    )
    assert result == expected


@pytest.mark.parametrize(
    ("name", "operations", "expected"),
    [
        (H, [H, H, H], H),
        (M, [H, H], M),
        (H, [H, M], M),
        (H, [H, H, L], M),  # a third is low: still medium
        (H, [H, L, L], L),  # more than a third
        (H, [], L),
    ],
)
def test_process_confidence(
    name: Confidence, operations: list[Confidence], expected: Confidence
) -> None:
    assert process_confidence(name, operations) == expected


def test_lowest() -> None:
    assert lowest([H, L, M]) == L and lowest([]) == M
