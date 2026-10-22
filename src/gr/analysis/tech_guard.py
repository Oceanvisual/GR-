"""Minimum sufficient technology and the flag requiring justification (FR-043, T037)."""

import tomllib
from collections.abc import Sequence
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gr.domain.enums import OperationType, ProblemPattern, SolutionCategory
from gr.domain.initiative import Finding, Initiative, TechGuard
from gr.domain.process import Operation


class ProblemRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: str = Field(min_length=1)
    min_category: SolutionCategory


class ProblemCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    patterns: dict[ProblemPattern, ProblemRule]
    operation_minima: dict[OperationType, SolutionCategory] = {}

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if missing := set(ProblemPattern) - self.patterns.keys():
            raise ValueError(f"missing problem patterns: {sorted(missing)}")
        categories = [r.min_category for r in self.patterns.values()]
        if SolutionCategory.NO_CHANGE in [*categories, *self.operation_minima.values()]:
            raise ValueError("no_change is not a technology for resolving a finding")
        return self


@cache
def load_catalog(path: Path | None = None) -> ProblemCatalog:
    """Bundled defaults, or an explicit TOML file; configuration is frozen until restart."""
    text = (
        path.read_text("utf-8")
        if path
        else resources.files("gr.config").joinpath("problem_catalog.toml").read_text("utf-8")
    )
    return ProblemCatalog.model_validate(tomllib.loads(text))


def evaluate(
    initiative: Initiative,
    findings: Sequence[Finding],
    *,
    operations: Sequence[Operation] = (),
    catalog: ProblemCatalog | None = None,
) -> TechGuard:
    """Take the highest required minimum across this initiative's findings and operations.

    Only linked findings count; the presence of a rationale never removes the flag. S9 uses
    it to apply the configurable feasibility penalty. Inputs are not modified.
    """
    if initiative.solution_category == SolutionCategory.NO_CHANGE:
        return TechGuard(min_category=SolutionCategory.NO_CHANGE, requires_justification=False)
    catalog = catalog or load_catalog()
    linked = set(initiative.finding_ids)
    selected = [f for f in findings if f.id in linked]
    if {f.id for f in selected} != linked:
        raise ValueError("initiative refers to an unknown finding")
    if len(selected) != len(linked):
        raise ValueError("finding ids must be unique")
    if any(f.process_id != initiative.process_id for f in selected):
        raise ValueError("initiative and findings must belong to the same process")
    minima = [catalog.patterns[f.pattern].min_category for f in selected]
    minima += [
        catalog.operation_minima[op.operation_type]
        for op in operations
        if op.id in initiative.operation_ids and op.operation_type in catalog.operation_minima
    ]
    minimum = max(minima, key=lambda category: category.rank)
    return TechGuard(
        min_category=minimum,
        requires_justification=initiative.solution_category.rank > minimum.rank,
    )
