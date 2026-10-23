"""Score each strategy's tree and pick the best (R-22). Deterministic; ties go to the earlier,
more specific strategy (regulation > layout > paragraphs)."""

from dataclasses import dataclass

from gr.structure.strategies import STRATEGIES, Strategy, StrategyOutcome
from gr.structure.tree import Unit

PARAGRAPHS_SCORE = 0.2  # the fallback: always available, never confident
LAYOUT_WEIGHT = 0.85  # formatting is weaker evidence than numbering
MIN_CONFIDENCE = 0.5  # STRUCTURE_MIN_CONFIDENCE: below it the user is warned
_DENSITY_SHARE = 0.03  # enough numbered markers for a numbered document
_HEADING_SHARE = (0.01, 0.5)  # a heading every other line is not a structure


@dataclass(frozen=True)
class Choice:
    outcome: StrategyOutcome
    confidence: float
    scores: dict[str, float]
    failed: dict[str, str]


def score(outcome: StrategyOutcome, total_units: int) -> float:
    if outcome.name == "paragraphs":
        return PARAGRAPHS_SCORE
    n = max(total_units, 1)
    coverage = outcome.covered / n
    if outcome.name == "regulation":
        if outcome.numbered == 0:
            return 0.0
        plausibility = outcome.numbered / (outcome.numbered + outcome.rejected)
        density = min(1.0, outcome.numbered / max(3.0, _DENSITY_SHARE * n))
        conflicts = 0.5 * outcome.duplicates / outcome.numbered
        value = coverage * plausibility * density - conflicts
    elif outcome.name == "layout":
        if outcome.headings < 2:
            return 0.0
        share = outcome.headings / n
        shape = 1.0 if _HEADING_SHARE[0] <= share <= _HEADING_SHARE[1] else 0.4
        # One heading over almost everything is not a structure.
        balance = min(1.0, 2 * (1 - outcome.largest_group / n))
        value = LAYOUT_WEIGHT * coverage * shape * balance
    else:
        value = 0.0
    return round(max(0.0, min(1.0, value)), 3)


def choose(units: list[Unit], strategies: dict[str, Strategy] | None = None) -> Choice:
    """Run every strategy; a failing one drops out with its reason recorded."""
    candidates = strategies or STRATEGIES
    outcomes: list[StrategyOutcome] = []
    failed: dict[str, str] = {}
    for name, strategy in candidates.items():
        try:
            outcomes.append(strategy(units))
        except Exception as exc:  # one broken strategy must not break parsing (FR-018)
            # Recorded in diagnostics.structure.failed_strategies; this layer does no logging.
            failed[name] = f"{type(exc).__name__}: {str(exc)[:200]}"
    if not outcomes:
        raise RuntimeError(f"every structure strategy failed: {failed}")
    scores = {o.name: score(o, len(units)) for o in outcomes}
    best = max(outcomes, key=lambda o: scores[o.name])  # max keeps the first of equals
    return Choice(outcome=best, confidence=scores[best.name], scores=scores, failed=failed)
