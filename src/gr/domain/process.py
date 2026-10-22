"""Process model extracted from the act (data-model.md §3)."""

from typing import Literal, Self

from pydantic import Field, model_validator

from gr.domain.base import DomainModel
from gr.domain.document import Citation
from gr.domain.enums import Confidence, DeadlineUnit, KnowledgeLayer, OperationType
from gr.domain.review import ReviewState

LEGALLY_SIGNIFICANT_TYPES = frozenset({OperationType.DECISION, OperationType.SIGNING})
# A process, an operation, a decision point: the act's facts put into a model, an
# interpretation (FR-093); each value with a basis carries its own layer.
Interpretation = Literal[KnowledgeLayer.INTERPRETATION]


class Grounded[T](DomainModel):
    """A value with its basis in the act and its knowledge layer (principle III)."""

    value: T
    citations: list[Citation] = []
    layer: KnowledgeLayer
    confidence: Confidence = Confidence.MEDIUM

    @model_validator(mode="after")
    def _fact_is_cited(self) -> Self:
        if self.layer == KnowledgeLayer.FACT and not self.citations:
            raise ValueError("a fact needs at least one citation")
        return self


class Role(DomainModel):
    id: str
    name: Grounded[str]


class DocumentRef(DomainModel):
    name: Grounded[str]
    mandatory: bool | None = None


class Deadline(DomainModel):
    raw: Grounded[str]
    value: int | None = Field(default=None, ge=0)
    unit: DeadlineUnit
    anchor: str | None = None
    parsed_from_quote: bool = False


class Operation(DomainModel):
    id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    name: str
    action: Grounded[str]
    object: str | None = None
    performer: Grounded[str] | None = None
    inputs: list[Grounded[str]] = []
    outputs: list[Grounded[str]] = []
    depends_on: list[str] = []
    deadline: Deadline | None = None
    conditions: list[Grounded[str]] = []
    information_systems: list[Grounded[str]] = []
    operation_type: OperationType
    legally_significant: bool = False
    handles_personal_data: bool = False
    is_loop_back: bool = False
    confidence: Confidence = Confidence.MEDIUM
    layer: Interpretation = KnowledgeLayer.INTERPRETATION
    review: ReviewState = ReviewState()

    @model_validator(mode="after")
    def _no_self_dependency(self) -> Self:
        if self.id in self.depends_on:
            raise ValueError(f"operation {self.id} depends on itself")
        return self


def derive_legally_significant(llm_flag: bool, operation_type: OperationType) -> bool:
    """Deterministic rule from data-model §3; only an expert may clear it, with a comment."""
    return llm_flag or operation_type in LEGALLY_SIGNIFICANT_TYPES


class DecisionOutcome(DomainModel):
    label: str = Field(min_length=1)
    next_operation_id: str | None = None
    ends_process: bool = False


class DecisionPoint(DomainModel):
    id: str = Field(min_length=1)
    after_operation_id: str
    condition: Grounded[str]
    outcomes: list[DecisionOutcome] = Field(min_length=2)
    criteria: list[Grounded[str]] = []
    layer: Interpretation = KnowledgeLayer.INTERPRETATION
    review: ReviewState = ReviewState()


IssueCode = Literal[
    "missing_result",
    "no_operations",
    "dangling_dependency",
    "cycle",
    "unverified_quote",
    "deadline_not_in_quote",
    "performer_not_in_quote",
    "duplicate_merged",
    "relocated_citation",
    "invalid_decision_point",
]


class ValidationIssue(DomainModel):
    code: IssueCode
    severity: Literal["error", "warning"]
    element_id: str
    message: str


class ValidationReport(DomainModel):
    issues: list[ValidationIssue] = []

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


class Process(DomainModel):
    # `no_operations` is a validation issue (S5), not a construction error, so an empty
    # extraction can still be stored and shown with its report.
    id: str = Field(min_length=1)
    name: Grounded[str]
    goal: Grounded[str] | None = None
    trigger: Grounded[str] | None = None
    participants: list[Role] = []
    inputs: list[DocumentRef] = []
    operations: list[Operation] = []
    decision_points: list[DecisionPoint] = []
    deadlines: list[Deadline] = []
    information_systems: list[Grounded[str]] = []
    result: Grounded[str] | None = None
    related_process_ids: list[str] = []
    validation: ValidationReport = ValidationReport()
    confidence: Confidence = Confidence.MEDIUM
    layer: Interpretation = KnowledgeLayer.INTERPRETATION
    review: ReviewState = ReviewState()

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [op.id for op in self.operations] + [dp.id for dp in self.decision_points]
        if len(ids) != len(set(ids)):
            raise ValueError(f"process {self.id}: duplicate operation or decision point id")
        return self


class CoverageReport(DomainModel):
    fragments_total: int = Field(ge=0)
    fragments_used: int = Field(ge=0)
    uncovered_action_fragments: list[str] = []
    non_process_fragments: list[str] = []

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if self.fragments_used > self.fragments_total:
            raise ValueError("fragments_used must not exceed fragments_total")
        return self


UnverifiedElement = Literal[
    "process_name",
    "goal",
    "trigger",
    "result",
    "participant",
    "input",
    "information_system",
    "deadline",
    "operation",
    "performer",
    "condition",
    "decision_point",
    "criterion",
]


class UnverifiedItem(DomainModel):
    """A claim of the model that did not pass verification: shown in «Не подтверждено», never
    used as a fact (principle I, FR-025)."""

    element_type: UnverifiedElement
    element_id: str
    process_id: str
    value: str
    citation: Citation | None = None
    reason: str


class ProcessModel(DomainModel):
    """The verified result of S3–S5 for one run (api.md `ProcessMap`, without the overlay)."""

    processes: list[Process] = []
    unverified: list[UnverifiedItem] = []
    coverage: CoverageReport
