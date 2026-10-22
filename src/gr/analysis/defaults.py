"""Default assumptions from the configurable profile (FR-050…FR-052, data-model.md §5, T043).

Numbers come from `config/assumption_profile.toml` by operation type, solution category and
the levels S6 gave; the LLM never produces them (FR-052). The conservative and optimistic
values are the base times the profile spreads, shares capped at 1.

Generation only fills keys the set does not have yet (llm.md § S6, validation 7): whatever is
there — a user value, a value from the document, an earlier default — stays as it is. A
derived default (review hours from the operation time, fixed opex from capex) is computed
from the current parent value, the user's when they set one. Defaults of elements that are
gone are dropped; user values are kept. Nothing new → the same set, no new version. A
regenerated initiative takes the user's values of the one it replaces (`predecessors`, FR-083)
before the profile's.
"""

import tomllib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gr.domain.assumptions import (
    Assumption,
    AssumptionScope,
    AssumptionSet,
    Triple,
    global_key,
    initiative_key,
    initiative_operation_key,
    initiative_operation_target,
    operation_key,
    process_key,
)
from gr.domain.enums import AssumptionSource, Level5, OperationType, SolutionCategory
from gr.domain.initiative import Initiative
from gr.domain.process import Operation, Process, ProcessModel

SYSTEM_USER = "system"
_ONE = Decimal(1)
_ZERO = Decimal(0)
# Parameters (and the adoption group of adoption_y1…y3) that may have a scenario spread.
_SPREADABLE = frozenset(
    {
        "hourly_cost",
        "overhead_ratio",
        "fte_hours_per_year",
        "volume_per_year",
        "case_share",
        "time_hours",
        "reduction_share",
        "review_hours",
        "capex",
        "fixed_opex_per_year",
        "variable_cost_per_exec",
        "adoption",
    }
)


def _table[K: StrEnum](name: str, table: Mapping[K, Decimal], keys: set[K]) -> None:
    """A profile table covers exactly the enum values it is looked up by."""
    if set(table) != keys:
        raise ValueError(f"{name}: expected keys {sorted(keys)}, got {sorted(table)}")
    if any(value < 0 for value in table.values()):
        raise ValueError(f"{name}: values must be non-negative")


class AssumptionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str = Field(min_length=1)
    hourly_cost: Decimal = Field(ge=0)
    overhead_ratio: Decimal = Field(ge=0, le=1)
    fte_hours_per_year: Decimal = Field(gt=0)
    horizon_years: int = Field(ge=1, le=3)
    volume_per_year: Decimal = Field(ge=0)
    case_share: Decimal = Field(ge=0, le=1)
    fixed_opex_share: Decimal = Field(ge=0)
    adoption: tuple[Decimal, Decimal, Decimal]
    time_hours: dict[OperationType, Decimal]
    reduction_share: dict[Level5, Decimal]
    review_share: dict[SolutionCategory, Decimal]
    capex: dict[SolutionCategory, Decimal]
    integration_multiplier: dict[Level5, Decimal]
    variable_cost_per_exec: dict[SolutionCategory, Decimal]
    spread: dict[str, tuple[Decimal, Decimal]] = {}  # [conservative, optimistic] multipliers

    @model_validator(mode="after")
    def _complete(self) -> Self:
        # no_change is a decision, not a technology: it costs nothing and has no entry.
        technologies = set(SolutionCategory) - {SolutionCategory.NO_CHANGE}
        _table("time_hours", self.time_hours, set(OperationType))
        _table("reduction_share", self.reduction_share, set(Level5))
        _table("integration_multiplier", self.integration_multiplier, set(Level5))
        _table("review_share", self.review_share, technologies)
        _table("capex", self.capex, technologies)
        _table("variable_cost_per_exec", self.variable_cost_per_exec, technologies)
        shares = [*self.reduction_share.values(), *self.review_share.values(), *self.adoption]
        if any(not _ZERO <= share <= _ONE for share in shares):
            raise ValueError("reduction, review and adoption shares must be within [0; 1]")
        if unknown := set(self.spread) - _SPREADABLE:
            raise ValueError(f"no spread allowed for {sorted(unknown)}")
        if any(multiplier <= 0 for pair in self.spread.values() for multiplier in pair):
            raise ValueError("spread multipliers must be positive")
        return self

    def triple(self, group: str, base: Decimal, *, share: bool = False) -> Triple:
        low, high = self.spread.get(group, (_ONE, _ONE))
        conservative, optimistic = base * low, base * high
        if share:
            conservative, base, optimistic = (
                min(conservative, _ONE),
                min(base, _ONE),
                min(optimistic, _ONE),
            )
        return Triple(conservative=conservative, base=base, optimistic=optimistic)


@cache
def load_profile(path: Path | None = None) -> AssumptionProfile:
    """Bundled defaults, or an explicit TOML file; numbers are read as exact decimals."""
    text = (
        path.read_text("utf-8")
        if path
        else resources.files("gr.config").joinpath("assumption_profile.toml").read_text("utf-8")
    )
    return AssumptionProfile.model_validate(tomllib.loads(text, parse_float=Decimal))


def operations_after_decisions(process: Process) -> set[str]:
    """Operations a decision may route cases away from: their share of cases depends on
    branch odds the act does not give (R-13).

    They follow a decision point: the successors of the operation it comes after and the
    targets of its outcomes, with their successors, later in execution order than the point.
    A dependency on a loop-back step does not extend a branch (rework returns to the flow).
    """
    by_id = {op.id: op for op in process.operations}
    successors: dict[str, set[str]] = {op.id: set() for op in process.operations}
    for op in process.operations:
        for dependency in op.depends_on:
            if dependency in by_id and not by_id[dependency].is_loop_back:
                successors[dependency].add(op.id)
    after: set[str] = set()
    for point in process.decision_points:
        origin = by_id.get(point.after_operation_id)
        if origin is None:
            continue
        stack = [*successors[origin.id]]
        stack += [o.next_operation_id for o in point.outcomes if o.next_operation_id in by_id]
        reached: set[str] = set()
        while stack:
            node = stack.pop()
            if node not in reached:
                reached.add(node)
                stack.extend(successors[node] - reached)
        after |= {node for node in reached if by_id[node].seq > origin.seq}
    return after


def _units(currency: str) -> dict[str, str]:
    share = "доля"
    return {
        "hourly_cost": f"{currency}/ч",
        "hourly_cost_override": f"{currency}/ч",
        "overhead_ratio": share,
        "fte_hours_per_year": "ч",
        "horizon_years": "лет",
        "volume_per_year": "случаев/год",
        "time_hours": "ч",
        "case_share": share,
        "reduction_share": share,
        "review_hours": "ч",
        "variable_cost_per_exec": currency,
        "capex": currency,
        "fixed_opex_per_year": f"{currency}/год",
        "adoption_y1": share,
        "adoption_y2": share,
        "adoption_y3": share,
    }


def _ru(value: Decimal) -> str:
    """A number inside a note, in the notation of the screens."""
    return format(value.normalize(), "f").replace(".", ",")


def assumption_unit(parameter: str, profile: AssumptionProfile | None = None) -> str:
    """The unit of a parameter, in the currency of the profile."""
    return _units((profile or load_profile()).currency)[parameter]


class _Builder:
    def __init__(
        self,
        profile: AssumptionProfile,
        existing: dict[str, Assumption],
        now: datetime,
        predecessors: Mapping[str, str],
    ):
        self.profile = profile
        self.existing = existing
        self.now = now
        self.predecessors = predecessors
        self.units = _units(profile.currency)
        self.items: dict[str, Assumption] = {}

    def inherited(self, key: str, target_id: str | None) -> Assumption | None:
        """The user's value of the same key of the initiative this one replaces, or of the one
        before it."""
        if not key.startswith("ini."):
            return None
        _, initiative, rest = key.split(".", 2)
        previous = self.predecessors.get(initiative)
        seen: set[str] = set()
        while previous is not None and previous not in seen:
            seen.add(previous)
            found = self.existing.get(f"ini.{previous}.{rest}")
            if found is not None and found.source == AssumptionSource.USER:
                return found.model_copy(update={"key": key, "target_id": target_id})
            previous = self.predecessors.get(previous)
        return None

    def add(
        self,
        key: str,
        scope: AssumptionScope,
        target_id: str | None,
        values: Triple,
        note: str,
        *,
        requires_input: bool = False,
    ) -> Assumption:
        item = (
            self.existing.get(key)
            or self.inherited(key, target_id)
            or Assumption(
                key=key,
                scope=scope,
                target_id=target_id,
                unit=self.units[key.rsplit(".", 1)[-1]],
                values=values,
                source=AssumptionSource.DEFAULT_PROFILE,
                requires_input=requires_input,
                note=note,
                updated_by=SYSTEM_USER,
                updated_at=self.now,
            )
        )
        self.items[key] = item
        return item

    def globals(self) -> None:
        p = self.profile
        note = "Профиль по умолчанию: замените данными организации."
        for parameter, value, share in (
            ("hourly_cost", p.hourly_cost, False),
            ("overhead_ratio", p.overhead_ratio, True),
            ("fte_hours_per_year", p.fte_hours_per_year, False),
        ):
            self.add(global_key(parameter), "global", None, p.triple(parameter, value, share=share),
                     note)  # fmt: skip
        horizon = Decimal(p.horizon_years)
        self.add(
            global_key("horizon_years"),
            "global",
            None,
            Triple(conservative=horizon, base=horizon, optimistic=horizon),
            note,
        )

    def process(self, process: Process) -> None:
        p = self.profile
        self.add(
            process_key(process.id, "volume_per_year"),
            "process",
            process.id,
            p.triple("volume_per_year", p.volume_per_year),
            "Объём не указан в НПА: задайте число случаев в год. Сейчас — заглушка профиля.",
            requires_input=True,
        )
        branched = operations_after_decisions(process)
        for op in process.operations:
            self.add(
                operation_key(op.id, "time_hours"),
                "operation",
                op.id,
                p.triple("time_hours", p.time_hours[op.operation_type]),
                "Профиль: время по типу операции.",
            )
            after = op.id in branched
            self.add(
                operation_key(op.id, "case_share"),
                "operation",
                op.id,
                p.triple("case_share", p.case_share, share=True),
                "Операция после точки решения: доля случаев зависит от исхода, НПА её не даёт."
                if after
                else "Операция выполняется во всех случаях процесса.",
                requires_input=after,
            )

    def initiative(self, initiative: Initiative, operations: dict[str, Operation]) -> None:
        p = self.profile
        category = initiative.solution_category
        none = category == SolutionCategory.NO_CHANGE
        level = initiative.ratings.integration_complexity.level
        multiplier = p.integration_multiplier[level]
        capex = self.add(
            initiative_key(initiative.id, "capex"),
            "initiative",
            initiative.id,
            p.triple("capex", _ZERO if none else p.capex[category] * multiplier),
            f"Профиль: стоимость категории решения × {_ru(multiplier)} за сложность интеграции.",
        )
        self.add(
            initiative_key(initiative.id, "fixed_opex_per_year"),
            "initiative",
            initiative.id,
            p.triple("fixed_opex_per_year", p.fixed_opex_share * capex.values.base),
            f"Профиль: {_ru(p.fixed_opex_share)} × стоимость внедрения в год.",
        )
        for year, share in enumerate(p.adoption, 1):
            self.add(
                initiative_key(initiative.id, f"adoption_y{year}"),
                "initiative",
                initiative.id,
                p.triple("adoption", share, share=True),
                f"Профиль: доля внедрения в {year}-й год.",
            )
        levels = {item.operation_id: item.level for item in initiative.reduction_potential}
        for op_id in initiative.operation_ids:
            if op_id not in operations:
                raise ValueError(f"initiative {initiative.id} refers to unknown operation {op_id}")
            target = initiative_operation_target(initiative.id, op_id)
            self.add(
                initiative_operation_key(initiative.id, op_id, "reduction_share"),
                "initiative_operation",
                target,
                p.triple(
                    "reduction_share",
                    _ZERO if none else p.reduction_share[levels[op_id]],
                    share=True,
                ),
                "Профиль: доля по уровню потенциала сокращения из анализа.",
            )
            time = self.items[operation_key(op_id, "time_hours")].values.base
            review_share = _ZERO if none else p.review_share[category]
            self.add(
                initiative_operation_key(initiative.id, op_id, "review_hours"),
                "initiative_operation",
                target,
                p.triple("review_hours", review_share * time),
                f"Профиль: {_ru(review_share)} × время операции на проверку результата.",
            )
            self.add(
                initiative_operation_key(initiative.id, op_id, "variable_cost_per_exec"),
                "initiative_operation",
                target,
                p.triple(
                    "variable_cost_per_exec", _ZERO if none else p.variable_cost_per_exec[category]
                ),
                "Профиль: переменные расходы по категории решения.",
            )

    def result(self) -> list[Assumption]:
        kept = [
            item
            for key, item in self.existing.items()
            if key not in self.items and item.source != AssumptionSource.DEFAULT_PROFILE
        ]
        return [*self.items.values(), *kept]


def build_assumptions(
    model: ProcessModel,
    initiatives: Sequence[Initiative],
    profile: AssumptionProfile | None = None,
    existing: AssumptionSet | None = None,
    *,
    run_id: str,
    user: str = SYSTEM_USER,
    now: datetime | None = None,
    predecessors: Mapping[str, str] | None = None,
) -> AssumptionSet:
    """The assumption set for the model and its initiatives; `existing` is the current one;
    `predecessors`: regenerated initiative → the one it replaced."""
    if existing is not None and existing.run_id != run_id:
        raise ValueError("existing assumptions belong to another run")
    now = now or datetime.now(UTC)
    builder = _Builder(
        profile or load_profile(), existing.by_key() if existing else {}, now, predecessors or {}
    )
    builder.globals()
    for process in model.processes:
        builder.process(process)
    operations = {op.id: op for process in model.processes for op in process.operations}
    for initiative in initiatives:
        builder.initiative(initiative, operations)
    items = builder.result()
    if existing is not None and {item.key: item for item in items} == existing.by_key():
        return existing
    return AssumptionSet(
        run_id=run_id,
        version=existing.version + 1 if existing else 1,
        items=items,
        created_by=user,
        created_at=now,
    )
