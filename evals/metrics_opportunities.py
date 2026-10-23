"""S6 metrics: the initiatives of a run against the quality criteria of llm.md § S6 (T066).
No LLM, no I/O; the judge (judge.py) adds the two criteria only a reader can tell.

- `s6_reference_integrity` — the share of the initiatives S6 proposed whose references all
  existed: a reference to an unknown operation, finding or fragment is cut by the validation
  (FR-047), and here it counts against the model;
- `s6_llm_for_classic_share` — among initiatives whose problem patterns need no more than
  classic automation, the share proposing an LLM or an agent (the bar: ≤ 20 %);
- `s6_simpler_alternative_share` — the share of initiatives that considered a simpler solution
  than their own (for classic automation: changing nothing);
- `s6_no_change_share` — the share of processes left as they are: reported, not gated, since
  either direction can be right.
"""

from collections.abc import Iterable

from pydantic import BaseModel

from gr.domain.enums import SolutionCategory
from gr.domain.initiative import InitiativeSet

ADVANCED = frozenset({SolutionCategory.LLM_RAG, SolutionCategory.AI_AGENT})
REFERENCE_ISSUES = frozenset({"unknown_operation", "unknown_finding", "unknown_fragment"})
# The validation drops the initiative: it never reaches the result.
DROPPED_ISSUES = frozenset(
    {"empty_initiative", "invalid_initiative", "missing_finding", "missing_provision"}
)

# Higher is better (True) or lower is better (False), as `metrics.QUALITY`.
S6_QUALITY: dict[str, bool] = {
    "s6_reference_integrity": True,
    "s6_llm_for_classic_share": False,
    "s6_simpler_alternative_share": True,
    "s6_judge_grounded": True,
    "s6_judge_concrete": True,
}
S6_INFO = ("s6_no_change_share", "s6_initiatives")


class PredInitiative(BaseModel):
    """What the metrics and the judge read of an initiative."""

    id: str
    process_id: str
    title: str
    description: str
    architecture_idea: str
    tech_rationale: str
    solution_category: SolutionCategory
    min_category: SolutionCategory | None  # the simplest sufficient one for its patterns
    patterns: list[str]
    alternatives: list[SolutionCategory]


class PredOpportunities(BaseModel):
    processes: int
    initiatives: list[PredInitiative] = []
    no_change: list[str] = []  # processes «оптимизация не требуется»
    # (code, process, S6 key) of the S7 validation: the keys of S6, not the ids of the result
    issues: list[tuple[str, str, str]] = []


def to_opportunities(result: InitiativeSet, processes: int) -> PredOpportunities:
    patterns = {finding.id: finding.pattern.value for finding in result.findings}
    return PredOpportunities(
        processes=processes,
        initiatives=[
            PredInitiative(
                id=i.id,
                process_id=i.process_id,
                title=i.title,
                description=i.description,
                architecture_idea=i.architecture_idea,
                tech_rationale=i.tech_rationale,
                solution_category=i.solution_category,
                min_category=i.tech_guard.min_category if i.tech_guard else None,
                patterns=sorted({patterns[f] for f in i.finding_ids if f in patterns}),
                alternatives=[a.category for a in i.alternatives_considered],
            )
            for i in result.initiatives
        ],
        no_change=sorted({n.process_id for n in result.no_change}),
        issues=[(issue.code, issue.process_id, issue.element) for issue in result.issues],
    )


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _proposed(pred: PredOpportunities) -> list[PredInitiative]:
    return [i for i in pred.initiatives if i.solution_category != SolutionCategory.NO_CHANGE]


def has_simpler_alternative(initiative: PredInitiative) -> bool:
    own = initiative.solution_category.rank
    return any(alternative.rank < own for alternative in initiative.alternatives)


def score_opportunities(pred: PredOpportunities) -> dict[str, float | None]:
    """The deterministic S6 metrics of one document; None where nothing was there to measure."""
    flawed = {(pid, key) for code, pid, key in pred.issues if code in REFERENCE_ISSUES}
    dropped = {(pid, key) for code, pid, key in pred.issues if code in DROPPED_ISSUES}
    proposed = len(pred.initiatives) + len(dropped)  # what S6 answered, kept or not
    classic = [i for i in pred.initiatives if i.min_category == SolutionCategory.CLASSIC_AUTOMATION]
    advanced = sum(1 for i in classic if i.solution_category in ADVANCED)
    initiatives = _proposed(pred)
    return {
        "s6_reference_integrity": _share(proposed - len(flawed), proposed),
        "s6_llm_for_classic_share": _share(advanced, len(classic)),
        "s6_simpler_alternative_share": _share(
            sum(1 for i in initiatives if has_simpler_alternative(i)), len(initiatives)
        ),
        "s6_no_change_share": _share(len(pred.no_change), pred.processes),
        "s6_initiatives": float(len(initiatives)),
    }


def judged(
    verdicts: Iterable[tuple[bool, bool]],
) -> dict[str, float | None]:
    """(grounded, concrete) of every initiative → the two judge metrics."""
    items = list(verdicts)
    return {
        "s6_judge_grounded": _share(sum(1 for g, _ in items if g), len(items)),
        "s6_judge_concrete": _share(sum(1 for _, c in items if c), len(items)),
    }
