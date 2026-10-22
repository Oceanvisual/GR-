"""Initiatives, assumptions and economics shared by the JSON API and the HTML screens.

Economics is computed on every read from the current assumption set (FR-056, R-13): nothing
is stored but the assumptions. An edit saves the next version of the set (If-Match against
lost updates), logs the change for history and returns the recomputed table, with no LLM
call. Keys of elements that appeared later are filled from the profile when the set is read.
Everything is computed from the analysis as the expert left it (data-model §8): rejected
elements are out of the calculations, edited ones count with their new values.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gr.analysis.defaults import SYSTEM_USER, assumption_unit, build_assumptions
from gr.analysis.economics import AssumptionValidationError, compute_solo
from gr.analysis.grouping import build_priorities
from gr.analysis.overlay import EffectiveState
from gr.analysis.portfolio import build_portfolio
from gr.analysis.scoring import load_priority_config, score_initiatives
from gr.api.deps import AppState
from gr.domain.assumptions import Assumption, AssumptionSet, process_key
from gr.domain.economics import EconomicsResult, Effect, InitiativeEconomics, PortfolioEconomics
from gr.domain.enums import (
    AssumptionSource,
    LegalGate,
    PriorityGroup,
    ProblemPattern,
    ReviewStatus,
    Scenario,
)
from gr.domain.ids import new_id
from gr.domain.initiative import Finding, Initiative, NoChangeDecision, PostprocessIssue
from gr.domain.priority import PriorityConfig, PriorityConfigVersion, PriorityResult
from gr.domain.process import Operation, Process
from gr.domain.review import ReviewEvent
from gr.errors import AppError
from gr.services.analyses import AnalysisService, Outputs
from gr.storage.repositories import ConflictError

MODEL_DISCLAIMER = (
    "Суммы — денежный эквивалент высвобождаемого рабочего времени по модельным допущениям. "
    "Это не подтверждённый финансовый результат и не сокращение бюджета."
)
DEFAULTS_BANNER = (
    "В расчёте есть модельные допущения по умолчанию: они не подтверждены, замените их данными "
    "организации на экране «Настройки модели»."
)
INCOMPLETE_BANNER = "Анализ неполный: часть процессов или инициатив не обработана."
STALE_BANNER = (
    "Часть инициатив требует пересмотра: эксперт изменил процесс, операцию или проблему, "
    "на которых они построены."
)
# entity_id of assumption-set and priority-config events: one of each per run, any version.
ASSUMPTIONS_ENTITY = "assumptions"
PRIORITY_ENTITY = "priority_config"


class InitiativeRow(BaseModel):
    initiative: Initiative
    process_name: str
    finding_patterns: list[ProblemPattern]
    economics: InitiativeEconomics | None = None  # solo and attributed; None if excluded
    priority: PriorityResult | None = None  # None for an excluded initiative
    excluded_reason: str | None = None  # out of the calculations by the expert's decisions


class InitiativeTable(BaseModel):
    scenario: Scenario = Scenario.BASE
    assumptions_version: int | None = None
    priority_config_version: int | None = None
    rows: list[InitiativeRow]  # by rank, blocked last
    portfolio: PortfolioEconomics | None = None
    banners: list[str]
    no_change: list[NoChangeDecision]
    issues: list[PostprocessIssue]


class InitiativeDetail(BaseModel):
    initiative: Initiative
    process: Process
    operations: list[Operation]
    findings: list[Finding]
    history: list[ReviewEvent]
    issues: list[PostprocessIssue]
    economics: dict[Scenario, Effect] = {}  # solo effect in each scenario
    attributed: dict[Scenario, Effect] = {}  # its share of the portfolio, if in it
    in_portfolio: bool = False
    excluded_reason: str | None = None
    priority: PriorityResult | None = None  # breakdown, rank, group and its rule
    assumptions: list[Assumption] = []  # what the effect is computed from
    assumptions_version: int | None = None
    priority_config_version: int | None = None


class PriorityUpdate(BaseModel):
    config: PriorityConfig
    version: int
    initiatives: InitiativeTable


@dataclass(frozen=True)
class Evaluation:
    """Everything computed on read for a run: economics, priorities, portfolio."""

    effective: EffectiveState  # the analysis as the expert left it
    assumptions: AssumptionSet
    config: PriorityConfigVersion
    priorities: dict[str, PriorityResult]  # by initiative id; base scenario (FR-061)
    economics: dict[Scenario, EconomicsResult]  # with the portfolio of the scenario


class TripleIn(BaseModel):
    """Values as sent: the domain rules check them per key, so an error names the key."""

    model_config = ConfigDict(extra="forbid")
    conservative: Decimal = Field(allow_inf_nan=False)
    base: Decimal = Field(allow_inf_nan=False)
    optimistic: Decimal = Field(allow_inf_nan=False)


class AssumptionChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=300)
    values: TripleIn | None = None
    note: str | None = Field(default=None, max_length=2000)


class AssumptionsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[AssumptionChange] = Field(min_length=1, max_length=5000)
    comment: str | None = Field(default=None, max_length=2000)


class AssumptionsUpdate(BaseModel):
    assumption_set: AssumptionSet
    initiatives: InitiativeTable
    warnings: list[str]  # of the recomputed scenario: inputs required, scenarios out of order


def _reason(error: ValidationError) -> str:
    """The domain refusal of an assumption, in the user's words (FR-057)."""
    first = error.errors()[0]
    message = str(first.get("msg", ""))
    if first.get("type") == "greater_than_equal":
        return "значения не могут быть отрицательными"
    if "is a share" in message:
        return "доля должна быть от 0 до 1"
    if "same in all scenarios" in message:
        return "горизонт одинаков во всех сценариях"
    if "1, 2 or 3" in message:
        return "горизонт — 1, 2 или 3 года"
    return "некорректное значение"


def _checked[T](compute: Callable[..., T], *args: Any) -> T:
    """Run a calculation; assumptions it cannot use are a 400 with the keys."""
    try:
        return compute(*args)
    except AssumptionValidationError as exc:
        raise AppError(
            "validation_error", "Допущения не позволяют выполнить расчёт.", {"keys": exc.problems}
        ) from None


def _row_order(row: InitiativeRow) -> tuple[int, int, str]:
    """By rank; blocked ones (no rank) after them, rejected ones (no priority) last."""
    if row.priority is None:
        return (2, 0, row.initiative.id)
    if row.priority.rank is None:
        return (1, 0, row.initiative.id)
    return (0, row.priority.rank, row.initiative.id)


class ResultsService:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.analyses = AnalysisService(state)

    async def inputs(self, run_id: str) -> tuple[EffectiveState, AssumptionSet]:
        """What the economics of the run is computed from: the analysis as the expert left it
        and the current assumptions. Their keys follow the stored outputs, so a decision adds or
        drops none (a reopened operation finds its values again)."""
        outputs = await self.analyses.outputs(run_id, initiatives=True)
        return outputs.effective, await self._current(run_id, outputs)

    async def _current(self, run_id: str, outputs: Outputs) -> AssumptionSet:
        """The latest set, created from the profile or completed with keys of new elements."""
        repository = self.state.repos.assumptions
        latest = await repository.latest(run_id)
        built = build_assumptions(
            outputs.model, outputs.result.initiatives, existing=latest, run_id=run_id
        )
        if built is latest:
            return built
        try:
            await repository.add(built)
        except ConflictError:  # another request saved a version meanwhile: that one counts
            stored = await repository.latest(run_id)
            assert stored is not None
            return stored
        return built

    async def assumptions(self, run_id: str) -> AssumptionSet:
        _, current = await self.inputs(run_id)
        return current

    async def priority_config(self, run_id: str) -> PriorityConfigVersion:
        """The latest version; version 1 comes from config/priority_default.toml."""
        await self.analyses.get(run_id)
        repository = self.state.repos.priority_configs
        latest = await repository.latest(run_id)
        if latest is not None:
            return latest
        first = PriorityConfigVersion(
            run_id=run_id,
            version=1,
            config=load_priority_config(),
            created_by=SYSTEM_USER,
            created_at=datetime.now(UTC),
        )
        try:
            await repository.add(first)
        except ConflictError:
            stored = await repository.latest(run_id)
            assert stored is not None
            return stored
        return first

    async def evaluate(
        self, run_id: str, scenarios: Sequence[Scenario] = (Scenario.BASE,)
    ) -> Evaluation:
        """Solo economics of the base scenario → priorities → portfolio of each scenario
        (data-model §7). Nothing is stored, nothing calls the LLM."""
        effective, assumptions = await self.inputs(run_id)
        config = await self.priority_config(run_id)
        model, initiatives = effective.calculation_model(), effective.calculable()
        base = _checked(compute_solo, model, initiatives, assumptions, Scenario.BASE)
        priorities = build_priorities(
            score_initiatives(initiatives, base, config.config),
            config.config,
            assumptions_version=assumptions.version,
            config_version=config.version,
        )
        economics: dict[Scenario, EconomicsResult] = {}
        for scenario in scenarios:
            solo = (
                base
                if scenario == Scenario.BASE
                else _checked(compute_solo, model, initiatives, assumptions, scenario)
            )
            economics[scenario] = _checked(
                build_portfolio, initiatives, assumptions, solo, priorities
            )
        return Evaluation(
            effective=effective,
            assumptions=assumptions,
            config=config,
            priorities={p.initiative_id: p for p in priorities},
            economics=economics,
        )

    async def economics(self, run_id: str, scenario: Scenario = Scenario.BASE) -> EconomicsResult:
        return (await self.evaluate(run_id, [scenario])).economics[scenario]

    async def table(
        self,
        run_id: str,
        *,
        scenario: Scenario = Scenario.BASE,
        process_id: str | None = None,
        gate: LegalGate | None = None,
        group: PriorityGroup | None = None,
        include_rejected: bool = False,
    ) -> InitiativeTable:
        run = await self.analyses.get(run_id)
        found = await self.evaluate(run_id, [scenario])
        effective, current = found.effective, found.assumptions
        result = effective.result
        economics = found.economics[scenario]
        by_id = {e.initiative_id: e for e in economics.per_initiative}
        names = {p.id: p.name.value for p in effective.model.processes}
        finding_patterns = {f.id: f.pattern for f in result.findings}
        rows = [
            InitiativeRow(
                initiative=i,
                process_name=names.get(i.process_id, i.process_id),
                finding_patterns=sorted(
                    {finding_patterns[k] for k in i.finding_ids if k in finding_patterns}
                ),
                economics=by_id.get(i.id),
                priority=found.priorities.get(i.id),
                excluded_reason=effective.excluded.get(i.id),
            )
            for i in result.initiatives
            if (process_id is None or i.process_id == process_id)
            and (gate is None or (i.gate and i.gate.status == gate))
            and (include_rejected or i.review.status != ReviewStatus.REJECTED)
            and (
                group is None
                or (i.id in found.priorities and found.priorities[i.id].group == group)
            )
        ]
        rows.sort(key=_row_order)
        items = current.by_key()
        used = {key for e in economics.per_initiative for key in e.assumption_keys}
        banners = [MODEL_DISCLAIMER]
        if any(items[key].source == AssumptionSource.DEFAULT_PROFILE for key in used):
            banners.append(DEFAULTS_BANNER)
        banners += economics.warnings
        if any(row.initiative.stale for row in rows):
            banners.append(STALE_BANNER)
        if not run.completeness.complete:
            banners.append(INCOMPLETE_BANNER)
        return InitiativeTable(
            scenario=scenario,
            assumptions_version=current.version,
            priority_config_version=found.config.version,
            rows=rows,
            portfolio=economics.portfolio,
            banners=banners,
            issues=result.issues,
            no_change=[
                n for n in result.no_change if process_id is None or n.process_id == process_id
            ],
        )

    async def detail(self, run_id: str, initiative_id: str) -> InitiativeDetail:
        found = await self.evaluate(run_id, list(Scenario))
        effective, current = found.effective, found.assumptions
        item = next((i for i in effective.initiatives if i.id == initiative_id), None)
        if item is None:
            raise AppError("not_found", "Инициатива не найдена.")
        process = next((p for p in effective.model.processes if p.id == item.process_id), None)
        if process is None:
            raise AppError("not_found", "Процесс инициативы не найден.")
        own = {
            scenario: next(
                (e for e in economics.per_initiative if e.initiative_id == item.id), None
            )
            for scenario, economics in found.economics.items()
        }  # all None for an excluded initiative
        base = own[Scenario.BASE]
        items = current.by_key()
        return InitiativeDetail(
            initiative=item,
            process=process,
            operations=[o for o in process.operations if o.id in item.operation_ids],
            findings=[f for f in effective.findings if f.id in item.finding_ids],
            history=list(reversed(await self.state.repos.reviews.list_for_run(run_id, item.id))),
            issues=[i for i in effective.result.issues if i.process_id == item.process_id],
            economics={s: e.solo for s, e in own.items() if e},
            attributed={s: e.attributed for s, e in own.items() if e and e.attributed},
            in_portfolio=bool(base and base.in_portfolio),
            excluded_reason=effective.excluded.get(item.id, base.excluded_reason if base else None),
            priority=found.priorities.get(item.id),
            assumptions=[items[key] for key in base.assumption_keys] if base else [],
            assumptions_version=current.version,
            priority_config_version=found.config.version,
        )

    async def update_priority_config(
        self,
        run_id: str,
        config: PriorityConfig,
        *,
        version: int,
        user: str,
        scenario: Scenario = Scenario.BASE,
    ) -> PriorityUpdate:
        """Save weights and thresholds as the next version, if `version` is still current."""
        current = await self.priority_config(run_id)
        if version != current.version:
            raise AppError("version_conflict", details={"current_version": current.version})
        if config == current.config:  # nothing changed: no new version, no event
            table = await self.table(run_id, scenario=scenario)
            return PriorityUpdate(config=current.config, version=current.version, initiatives=table)
        now = datetime.now(UTC)
        saved = PriorityConfigVersion(
            run_id=run_id, version=current.version + 1, config=config, created_by=user,
            created_at=now,
        )  # fmt: skip
        try:
            await self.state.repos.priority_configs.add(saved)
        except ConflictError:
            latest = await self.state.repos.priority_configs.latest(run_id)
            raise AppError(
                "version_conflict",
                details={"current_version": latest.version if latest else current.version},
            ) from None
        before = current.config.model_dump(mode="json")
        after = config.model_dump(mode="json")
        changed = [name for name in after if after[name] != before[name]]
        await self.state.repos.reviews.add(
            ReviewEvent(
                id=new_id("rev"),
                run_id=run_id,
                entity_type="priority_config",
                entity_id=PRIORITY_ENTITY,
                action="edit",
                patch={name: after[name] for name in changed},
                previous={name: before[name] for name in changed},
                user=user,
                created_at=now,
            )
        )
        table = await self.table(run_id, scenario=scenario)
        return PriorityUpdate(config=config, version=saved.version, initiatives=table)

    async def update_assumptions(
        self,
        run_id: str,
        patch: AssumptionsPatch,
        *,
        version: int,
        user: str,
        scenario: Scenario = Scenario.BASE,
    ) -> AssumptionsUpdate:
        """Save the changes as the next version, if `version` is still the current one."""
        effective, current = await self.inputs(run_id)
        if version != current.version:
            raise AppError("version_conflict", details={"current_version": current.version})
        now = datetime.now(UTC)
        items = current.by_key()
        processes = {p.id for p in effective.model.processes}
        problems: dict[str, str] = {}
        changed: dict[str, Assumption] = {}
        patch_log: dict[str, Any] = {}
        previous_log: dict[str, Any] = {}
        for change in patch.changes:
            key = change.key
            if key in changed or key in problems:
                problems[key] = "ключ указан дважды"
                continue
            if change.values is None and change.note is None:
                problems[key] = "нет изменений"
                continue
            old = items.get(key)
            if old is None and not _new_rate_allowed(key, processes, change):
                problems[key] = "неизвестное допущение"
                continue
            update: dict[str, Any] = {"updated_by": user, "updated_at": now}
            if change.values is not None:
                update |= {
                    "values": change.values.model_dump(),
                    "source": AssumptionSource.USER,
                    "source_citation": None,  # a quote of the act backs its number, not this one
                    "requires_input": False,
                }
            if change.note is not None:
                update["note"] = change.note.strip() or None
            elif change.values is not None and old and old.source != AssumptionSource.USER:
                update["note"] = None  # how the profile got its number says nothing of this one
            base = old.model_dump() if old else _new_rate(key)
            try:
                changed[key] = Assumption.model_validate(base | update)
            except ValidationError as exc:
                problems[key] = _reason(exc)
                continue
            patch_log[key] = _logged(changed[key])
            previous_log[key] = _logged(old) if old else None
        if problems:
            raise AppError("validation_error", "Некорректные допущения.", {"keys": problems})
        new_items = [changed.pop(i.key, i) for i in current.items] + list(changed.values())
        updated = AssumptionSet(
            run_id=run_id,
            version=current.version + 1,
            items=new_items,
            created_by=user,
            created_at=now,
        )
        # A value may break the calculation in some scenario (a zero working year of an FTE):
        # check all three before saving.
        computed: dict[Scenario, EconomicsResult] = {}
        model, initiatives = effective.calculation_model(), effective.calculable()
        for each in Scenario:
            try:
                computed[each] = compute_solo(model, initiatives, updated, each)
            except AssumptionValidationError as exc:
                problems |= exc.problems
        if problems:
            raise AppError("validation_error", "Некорректные допущения.", {"keys": problems})
        try:
            await self.state.repos.assumptions.add(updated)
        except ConflictError:
            latest = await self.state.repos.assumptions.latest(run_id)
            raise AppError(
                "version_conflict",
                details={"current_version": latest.version if latest else current.version},
            ) from None
        await self.state.repos.reviews.add(
            ReviewEvent(
                id=new_id("rev"),
                run_id=run_id,
                entity_type="assumption_set",
                entity_id=ASSUMPTIONS_ENTITY,
                action="edit",
                patch=patch_log,
                previous=previous_log,
                comment=patch.comment,
                user=user,
                created_at=now,
            )
        )
        table = await self.table(run_id, scenario=scenario)
        return AssumptionsUpdate(
            assumption_set=updated, initiatives=table, warnings=computed[scenario].warnings
        )


def _new_rate_allowed(key: str, processes: set[str], change: AssumptionChange) -> bool:
    """The only key a user may add: the hourly rate of an existing process (FR-050)."""
    parts = key.split(".")
    return (
        len(parts) == 3
        and parts[0] == "proc"
        and parts[2] == "hourly_cost_override"
        and parts[1] in processes
        and change.values is not None
    )


def _new_rate(key: str) -> dict[str, Any]:
    process_id = key.split(".")[1]
    zero = {"conservative": 0, "base": 0, "optimistic": 0}
    return {
        "key": process_key(process_id, "hourly_cost_override"),
        "scope": "process",
        "target_id": process_id,
        "unit": assumption_unit("hourly_cost_override"),
        "values": zero,
        "source": AssumptionSource.USER,
    }


def _logged(item: Assumption) -> dict[str, Any]:
    return {"values": item.values.model_dump(mode="json"), "note": item.note}
