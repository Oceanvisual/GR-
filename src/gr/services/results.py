"""Effective initiative views shared by JSON API and HTML screens (T042)."""

from pydantic import BaseModel

from gr.api.deps import AppState
from gr.domain.enums import LegalGate, ProblemPattern, ReviewStatus, Scenario, StageName
from gr.domain.initiative import (
    Finding,
    Initiative,
    InitiativeSet,
    NoChangeDecision,
    PostprocessIssue,
)
from gr.domain.process import Operation, Process
from gr.domain.review import ReviewEvent
from gr.errors import AppError
from gr.services.analyses import AnalysisService

MODEL_DISCLAIMER = (
    "Суммы — денежный эквивалент высвобождаемого рабочего времени по модельным допущениям. "
    "Это не подтверждённый финансовый результат и не сокращение бюджета."
)


class InitiativeRow(BaseModel):
    initiative: Initiative
    process_name: str
    finding_patterns: list[ProblemPattern]
    economics: None = None
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
        result = await self.initiative_set(run_id)
        model = await self.analyses.processes(run_id)
        names = {p.id: p.name.value for p in model.processes}
        finding_patterns = {f.id: f.pattern for f in result.findings}
        rows = [
            InitiativeRow(
                initiative=i,
                process_name=names.get(i.process_id, i.process_id),
                finding_patterns=sorted(
                    {finding_patterns[k] for k in i.finding_ids if k in finding_patterns}
                ),
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
        banners = [MODEL_DISCLAIMER]
        if not run.completeness.complete:
            banners.append("Анализ неполный: часть процессов или инициатив не обработана.")
        return InitiativeTable(
            scenario=scenario,
            rows=rows,
            banners=banners,
            issues=result.issues,
            no_change=[
                n for n in result.no_change if process_id is None or n.process_id == process_id
            ],
        )

    async def detail(self, run_id: str, initiative_id: str) -> InitiativeDetail:
        result = await self.initiative_set(run_id)
        item = next((i for i in result.initiatives if i.id == initiative_id), None)
        if item is None:
            raise AppError("not_found", "Инициатива не найдена.")
        model = await self.analyses.processes(run_id)
        process = next((p for p in model.processes if p.id == item.process_id), None)
        if process is None:
            raise AppError("not_found", "Процесс инициативы не найден.")
        return InitiativeDetail(
            initiative=item,
            process=process,
            operations=[o for o in process.operations if o.id in item.operation_ids],
            findings=[f for f in result.findings if f.id in item.finding_ids],
            history=list(reversed(await self.state.repos.reviews.list_for_run(run_id, item.id))),
            issues=[i for i in result.issues if i.process_id == item.process_id],
        )
