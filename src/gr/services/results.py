"""Initiatives, assumptions and economics shared by the JSON API and the HTML screens.

Economics is computed on every read from the current assumption set (FR-056, R-13): nothing
is stored but the assumptions. An edit saves the next version of the set (If-Match against
lost updates), logs the change for history and returns the recomputed table, with no LLM
call. Keys of elements that appeared later are filled from the profile when the set is read.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gr.analysis.defaults import assumption_unit, build_assumptions
from gr.analysis.economics import AssumptionValidationError, compute_solo
from gr.api.deps import AppState
from gr.domain.assumptions import Assumption, AssumptionSet, process_key
from gr.domain.economics import EconomicsResult, Effect, InitiativeEconomics
from gr.domain.enums import (
    AssumptionSource,
    LegalGate,
    ProblemPattern,
    ReviewStatus,
    Scenario,
    StageName,
)
from gr.domain.ids import new_id
from gr.domain.initiative import (
    Finding,
    Initiative,
    InitiativeSet,
    NoChangeDecision,
    PostprocessIssue,
)
from gr.domain.process import Operation, Process, ProcessModel
from gr.domain.review import ReviewEvent
from gr.errors import AppError
from gr.services.analyses import AnalysisService
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
# entity_id of assumption-set events: a run has one set, whatever its version.
ASSUMPTIONS_ENTITY = "assumptions"


class InitiativeRow(BaseModel):
    initiative: Initiative
    process_name: str
    finding_patterns: list[ProblemPattern]
    economics: InitiativeEconomics | None = None  # solo; None for a rejected initiative
    priority: None = None


class InitiativeTable(BaseModel):
    scenario: Scenario = Scenario.BASE
    assumptions_version: int | None = None
    priority_config_version: int | None = None
    rows: list[InitiativeRow]
    portfolio: None = None
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
    assumptions: list[Assumption] = []  # what the effect is computed from
    assumptions_version: int | None = None


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


def _compute(
    model: ProcessModel,
    initiatives: list[Initiative],
    assumptions: AssumptionSet,
    scenario: Scenario,
) -> EconomicsResult:
    try:
        return compute_solo(model, initiatives, assumptions, scenario)
    except AssumptionValidationError as exc:
        raise AppError(
            "validation_error", "Допущения не позволяют выполнить расчёт.", {"keys": exc.problems}
        ) from None


class ResultsService:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.analyses = AnalysisService(state)

    async def initiative_set(self, run_id: str) -> InitiativeSet:
        await self.analyses.get(run_id)
        output = await self.state.repos.stage_outputs.get(run_id, StageName.POSTPROCESS)
        if output is None:
            raise AppError("not_found", "Инициативы ещё не готовы.")
        return InitiativeSet.model_validate(output)

    async def inputs(self, run_id: str) -> tuple[ProcessModel, InitiativeSet, AssumptionSet]:
        """What the economics of the run is computed from: processes, initiatives, assumptions."""
        result = await self.initiative_set(run_id)
        model = await self.analyses.processes(run_id)
        return model, result, await self._current(run_id, model, result)

    async def _current(
        self, run_id: str, model: ProcessModel, result: InitiativeSet
    ) -> AssumptionSet:
        """The latest set, created from the profile or completed with keys of new elements."""
        repository = self.state.repos.assumptions
        latest = await repository.latest(run_id)
        built = build_assumptions(model, result.initiatives, existing=latest, run_id=run_id)
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
        _, _, current = await self.inputs(run_id)
        return current

    async def economics(self, run_id: str, scenario: Scenario = Scenario.BASE) -> EconomicsResult:
        model, result, current = await self.inputs(run_id)
        return _compute(model, result.initiatives, current, scenario)

    async def table(
        self,
        run_id: str,
        *,
        scenario: Scenario = Scenario.BASE,
        process_id: str | None = None,
        gate: LegalGate | None = None,
        include_rejected: bool = False,
    ) -> InitiativeTable:
        run = await self.analyses.get(run_id)
        model, result, current = await self.inputs(run_id)
        economics = _compute(model, result.initiatives, current, scenario)
        solo = {e.initiative_id: e for e in economics.per_initiative}
        names = {p.id: p.name.value for p in model.processes}
        finding_patterns = {f.id: f.pattern for f in result.findings}
        rows = [
            InitiativeRow(
                initiative=i,
                process_name=names.get(i.process_id, i.process_id),
                finding_patterns=sorted(
                    {finding_patterns[k] for k in i.finding_ids if k in finding_patterns}
                ),
                economics=solo.get(i.id),
            )
            for i in result.initiatives
            if (process_id is None or i.process_id == process_id)
            and (gate is None or (i.gate and i.gate.status == gate))
            and (include_rejected or i.review.status != ReviewStatus.REJECTED)
        ]
        rows.sort(
            key=lambda r: (
                bool(r.initiative.gate and r.initiative.gate.status == LegalGate.BLOCKED),
                r.initiative.id,
            )
        )
        items = current.by_key()
        used = {key for e in economics.per_initiative for key in e.assumption_keys}
        banners = [MODEL_DISCLAIMER]
        if any(items[key].source == AssumptionSource.DEFAULT_PROFILE for key in used):
            banners.append(DEFAULTS_BANNER)
        banners += economics.warnings
        if not run.completeness.complete:
            banners.append(INCOMPLETE_BANNER)
        return InitiativeTable(
            scenario=scenario,
            assumptions_version=current.version,
            rows=rows,
            banners=banners,
            issues=result.issues,
            no_change=[
                n for n in result.no_change if process_id is None or n.process_id == process_id
            ],
        )

    async def detail(self, run_id: str, initiative_id: str) -> InitiativeDetail:
        model, result, current = await self.inputs(run_id)
        item = next((i for i in result.initiatives if i.id == initiative_id), None)
        if item is None:
            raise AppError("not_found", "Инициатива не найдена.")
        process = next((p for p in model.processes if p.id == item.process_id), None)
        if process is None:
            raise AppError("not_found", "Процесс инициативы не найден.")
        by_scenario = {s: _compute(model, [item], current, s).per_initiative for s in Scenario}
        computed = by_scenario[Scenario.BASE]  # empty for a rejected initiative
        items = current.by_key()
        return InitiativeDetail(
            initiative=item,
            process=process,
            operations=[o for o in process.operations if o.id in item.operation_ids],
            findings=[f for f in result.findings if f.id in item.finding_ids],
            history=list(reversed(await self.state.repos.reviews.list_for_run(run_id, item.id))),
            issues=[i for i in result.issues if i.process_id == item.process_id],
            economics={s: found[0].solo for s, found in by_scenario.items() if found},
            assumptions=[items[key] for key in computed[0].assumption_keys] if computed else [],
            assumptions_version=current.version,
        )

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
        model, result, current = await self.inputs(run_id)
        if version != current.version:
            raise AppError("version_conflict", details={"current_version": current.version})
        now = datetime.now(UTC)
        items = current.by_key()
        processes = {p.id for p in model.processes}
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
        for each in Scenario:
            try:
                computed[each] = compute_solo(model, result.initiatives, updated, each)
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
