"""LLM answer schemas of S3 discover, S4 detail and S6 opportunities (contracts/llm.md, R-08).

Shaped by the T074 spike:
- no nullable fields: an absent string is "", an absent number is -1, an optional object is a
  list of at most one; large schemas with `anyOf [X, null]` are refused by strict decoding;
- every optional field has a default, so in json_object mode an omitted key does not reject
  the whole answer;
- quotes are required for facts only (action, performer, deadline, condition, result), not for
  every input and output: the answer for one process already approaches the token limit;
- keys of the grounded value are short, they repeat hundreds of times.

Fragment references are short aliases (`F12`) that the mapper turns back into fragment ids.
The schemas carry no dict fields: strict decoding cannot express them (llm/cache.py).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gr.domain.enums import (
    AutomationLevel,
    DeadlineUnit,
    DurationBucket,
    Level5,
    OperationType,
    ProblemPattern,
    RegulatoryMode,
    SolutionCategory,
)

NO_NUMBER = -1


class LLMSchema(BaseModel):
    # Unknown keys from a json_object answer are dropped, not fatal.
    model_config = ConfigDict(extra="ignore")


class Fact(LLMSchema):
    """A value with its basis: the fragment alias and a verbatim quote from it."""

    text: str = Field(description="значение своими словами, кратко")
    ref: str = Field(description="алиас фрагмента, например F12")
    quote: str = Field(description="дословная цитата из этого фрагмента, до 250 знаков")


# --- S3 discover ---------------------------------------------------------------------------


class ProcessCandidate(LLMSchema):
    key: str = Field(description="локальный ключ: p1, p2…")
    name: str
    trigger: str = Field(default="", description="что запускает процесс; пусто, если не сказано")
    result: str = Field(default="", description="результат процесса; пусто, если не сказано")
    core_refs: list[str] = Field(description="алиасы фрагментов с действиями процесса, ≥ 1")
    related_refs: list[str] = Field(
        default_factory=list, description="сроки, перечни документов, основания отказа"
    )
    evidence: Fact = Field(description="цитата, подтверждающая, что процесс есть в акте")
    parent_key: str = Field(default="", description="ключ процесса, частью которого является")


class DiscoveryOut(LLMSchema):
    processes: list[ProcessCandidate]
    non_process_refs: list[str] = Field(
        default_factory=list, description="общие положения, требования к помещениям и т. п."
    )
    notes: str = ""


# --- S4 detail -----------------------------------------------------------------------------


class DeadlineOut(LLMSchema):
    raw: Fact = Field(description="формулировка срока с цитатой")
    value: int = Field(default=NO_NUMBER, description="число из цитаты; -1, если числа нет")
    unit: DeadlineUnit
    anchor: str = Field(default="", description="от какого события отсчитывается")


class InputDocument(LLMSchema):
    name: Fact
    mandatory: Literal["yes", "no", "unknown"] = "unknown"


class ProcessOut(LLMSchema):
    name: Fact
    goal: list[Fact] = Field(default_factory=list, description="0 или 1 элемент")
    trigger: list[Fact] = Field(default_factory=list, description="0 или 1 элемент")
    result: list[Fact] = Field(default_factory=list, description="0 или 1 элемент")
    participants: list[Fact] = Field(default_factory=list)
    inputs: list[InputDocument] = Field(default_factory=list)
    information_systems: list[Fact] = Field(default_factory=list)
    deadlines: list[DeadlineOut] = Field(default_factory=list)


class OperationOut(LLMSchema):
    key: str = Field(description="o1, o2… в порядке выполнения")
    name: str = Field(description="краткое название операции")
    action: Fact
    object: str = ""
    performer: list[Fact] = Field(default_factory=list, description="0 или 1 элемент")
    inputs: list[str] = Field(default_factory=list, description="документы и данные на входе")
    outputs: list[str] = Field(default_factory=list, description="результаты операции")
    depends_on: list[str] = Field(default_factory=list, description="ключи предшествующих операций")
    deadline: list[DeadlineOut] = Field(default_factory=list, description="0 или 1 элемент")
    conditions: list[Fact] = Field(default_factory=list)
    information_systems: list[str] = Field(default_factory=list)
    operation_type: OperationType = OperationType.OTHER
    legally_significant: bool = False
    handles_personal_data: bool = False
    is_loop_back: bool = False


class OutcomeOut(LLMSchema):
    label: str
    next_operation: str = Field(default="", description="ключ операции; пусто, если конец")
    ends_process: bool = False


class DecisionPointOut(LLMSchema):
    key: str = Field(description="d1, d2…")
    after_operation: str = Field(description="ключ операции, после которой принимается решение")
    condition: Fact
    outcomes: list[OutcomeOut] = Field(description="не меньше двух исходов")
    criteria: list[Fact] = Field(default_factory=list, description="например, основания отказа")


class DetailOut(LLMSchema):
    process: ProcessOut
    operations: list[OperationOut]
    decision_points: list[DecisionPointOut] = Field(default_factory=list)


# --- S6 opportunities ----------------------------------------------------------------------


class FindingOut(LLMSchema):
    key: str = Field(description="f1, f2…")
    pattern: ProblemPattern
    operation_ids: list[str]
    description: str


class AlternativeOut(LLMSchema):
    category: SolutionCategory
    why_not: str


class ProvisionOut(LLMSchema):
    ref: str = Field(description="алиас фрагмента акта, который нужно изменить")
    change_nature: str


class ReductionOut(LLMSchema):
    operation_id: str
    level: Level5
    justification: str


class RatingOut(LLMSchema):
    level: Level5
    justification: str


class DurationOut(LLMSchema):
    bucket: DurationBucket
    justification: str


class RatingsOut(LLMSchema):
    feasibility: RatingOut
    data_availability: RatingOut
    integration_complexity: RatingOut
    legal_risk: RatingOut
    operational_risk: RatingOut
    duration: DurationOut


class InitiativeOut(LLMSchema):
    key: str = Field(description="i1, i2…")
    title: str
    finding_keys: list[str]
    operation_ids: list[str]
    solution_category: SolutionCategory
    description: str
    architecture_idea: str = ""
    tech_rationale: str = Field(description="почему не подходит более простое решение")
    alternatives_considered: list[AlternativeOut] = Field(default_factory=list)
    regulatory_mode: RegulatoryMode = RegulatoryMode.WITHIN_CURRENT
    provisions_to_revise: list[ProvisionOut] = Field(default_factory=list)
    automation_level: AutomationLevel = AutomationLevel.ASSISTIVE
    reduction_potential: list[ReductionOut] = Field(description="по каждой операции инициативы")
    ratings: RatingsOut
    risks: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class NoChangeOut(LLMSchema):
    applies: bool = False
    justification: str = ""


class OpportunitiesOut(LLMSchema):
    findings: list[FindingOut] = Field(default_factory=list)
    initiatives: list[InitiativeOut] = Field(default_factory=list)
    no_change: NoChangeOut = Field(default_factory=NoChangeOut)


STAGE_SCHEMAS: tuple[type[LLMSchema], ...] = (DiscoveryOut, DetailOut, OpportunitiesOut)
