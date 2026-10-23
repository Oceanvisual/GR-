"""Findings and optimization initiatives (data-model.md §4)."""

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from gr.domain.base import DomainModel
from gr.domain.enums import (
    AutomationLevel,
    DurationBucket,
    InitiativeRelation,
    KnowledgeLayer,
    LegalGate,
    Level5,
    ProblemPattern,
    RegulatoryMode,
    SolutionCategory,
)
from gr.domain.review import ReviewState


class Finding(DomainModel):
    id: str = Field(min_length=1)
    process_id: str
    operation_ids: list[str] = Field(min_length=1)
    pattern: ProblemPattern
    description: str
    layer: Literal[KnowledgeLayer.HYPOTHESIS] = KnowledgeLayer.HYPOTHESIS
    review: ReviewState = ReviewState()


class AlternativeConsidered(DomainModel):
    category: SolutionCategory
    why_not: str = Field(min_length=1)


class ProvisionToRevise(DomainModel):
    fragment_id: str = Field(min_length=1)
    change_nature: str = Field(min_length=1)


class ReductionPotential(DomainModel):
    operation_id: str
    level: Level5
    justification: str


class Rating(DomainModel):
    level: Level5
    justification: str


class DurationRating(DomainModel):
    bucket: DurationBucket
    justification: str


class Ratings(DomainModel):
    feasibility: Rating
    data_availability: Rating
    integration_complexity: Rating
    legal_risk: Rating
    operational_risk: Rating
    duration: DurationRating


class TechGuard(DomainModel):
    min_category: SolutionCategory
    requires_justification: bool


class Gate(DomainModel):
    status: LegalGate
    reasons: list[str] = []


class Relation(DomainModel):
    initiative_id: str
    relation: InitiativeRelation
    source: Literal["auto", "expert"] = "auto"


class Initiative(DomainModel):
    id: str = Field(min_length=1)
    process_id: str
    finding_ids: list[str] = []
    operation_ids: list[str] = Field(min_length=1)
    title: str
    description: str
    solution_category: SolutionCategory
    architecture_idea: str
    tech_rationale: str
    alternatives_considered: list[AlternativeConsidered] = []
    regulatory_mode: RegulatoryMode
    provisions_to_revise: list[ProvisionToRevise] = []
    requires_legal_review: bool = False
    automation_level: AutomationLevel
    automation_level_proposed: AutomationLevel
    autonomy_capped: bool = False
    reduction_potential: list[ReductionPotential]
    ratings: Ratings
    risks: list[str] = []
    limitations: list[str] = []
    # Computed in S7 (tech_guard.py, gating.py, relations.py); None until then.
    tech_guard: TechGuard | None = None
    gate: Gate | None = None
    relations: list[Relation] = []
    layer: Literal[KnowledgeLayer.HYPOTHESIS] = KnowledgeLayer.HYPOTHESIS
    stale: bool = False
    review: ReviewState = ReviewState()

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        changes_act = self.regulatory_mode == RegulatoryMode.REQUIRES_CHANGE
        if changes_act and not self.requires_legal_review:
            raise ValueError("requires_change implies requires_legal_review")
        if changes_act and not self.provisions_to_revise:
            raise ValueError("requires_change needs at least one provision to revise")
        if self.solution_category != SolutionCategory.NO_CHANGE:
            if not self.finding_ids:
                raise ValueError("an initiative needs at least one finding")
            if not self.alternatives_considered:
                raise ValueError("an initiative needs at least one considered alternative")
        if len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("operation_ids must be unique")
        covered = [item.operation_id for item in self.reduction_potential]
        if sorted(covered) != sorted(self.operation_ids):
            raise ValueError("reduction_potential must cover each operation exactly once")
        if self.autonomy_capped and not (
            self.automation_level_proposed == AutomationLevel.AUTONOMOUS
            and self.automation_level == AutomationLevel.HUMAN_APPROVAL
        ):
            raise ValueError("autonomy_capped means autonomous was lowered to human_approval")
        return self


class NoChangeDecision(DomainModel):
    process_id: str
    justification: str = Field(min_length=1)
    layer: Literal[KnowledgeLayer.HYPOTHESIS] = KnowledgeLayer.HYPOTHESIS


class PostprocessIssue(DomainModel):
    code: str
    process_id: str
    element: str
    message: str


class InitiativeSet(DomainModel):
    findings: list[Finding] = []
    initiatives: list[Initiative] = []
    no_change: list[NoChangeDecision] = []
    issues: list[PostprocessIssue] = []


class InitiativeVersion(DomainModel):
    """The initiatives of one process made again on request (FR-083): the output of S7 of a
    regeneration run. Version 1 is the analysis itself; the latest version is the effective one."""

    process_id: str
    version: int = Field(ge=2)
    based_on: datetime  # the expert's decisions made before this moment are in its model
    result: InitiativeSet  # this process only
    # New initiative → the one it replaces, with the same operations and solution category:
    # the user's assumptions of the old one carry over (data-model §8).
    predecessors: dict[str, str] = {}
    without_successor: list[str] = []  # replaced initiatives with no such counterpart

    @model_validator(mode="after")
    def _one_process(self) -> Self:
        result = self.result
        found = {f.process_id for f in result.findings} | {i.process_id for i in result.initiatives}
        found |= {n.process_id for n in result.no_change} | {i.process_id for i in result.issues}
        if found - {self.process_id}:
            raise ValueError("a version holds the initiatives of its own process only")
        ids = {i.id for i in result.initiatives}
        if set(self.predecessors) - ids:
            raise ValueError("predecessors name initiatives of this version")
        return self
