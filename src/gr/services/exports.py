"""Exports of an analysis (api.md § Экспорт, T054): the full JSON and the two CSV tables.

They read what the screens read, as the expert left it, so an export says what the screens
say: the CSV of initiatives is the table of the same scenario, the JSON adds everything the
table is computed from and the economics of all three scenarios. Nothing calls the LLM.
"""

from datetime import UTC, datetime

from gr.api.deps import AppState
from gr.domain.enums import Scenario
from gr.export.csv_export import initiatives_csv, operations_csv
from gr.export.json_export import AnalysisExport, build_export
from gr.services.results import ResultsService


class ExportService:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.results = ResultsService(state)

    async def analysis(self, run_id: str, scenario: Scenario, user: str) -> AnalysisExport:
        found = await self.results.evaluate(run_id, list(Scenario))
        table = await self.results.table(run_id, scenario=scenario, include_rejected=True)
        outputs = found.outputs
        repos = self.state.repos
        stored = outputs.stored
        return build_export(
            document=await repos.documents.get(outputs.run.doc_id),
            fragments=await repos.fragments.list_for_document(outputs.run.doc_id),
            run=outputs.run,
            effective=outputs.effective,
            versions={p.id: stored.version_of(p.id) for p in outputs.model.processes},
            assumptions=found.assumptions,
            config=found.config,
            economics=found.economics,
            priorities=list(found.priorities.values()),
            reviews=outputs.events,
            disclaimers=table.banners,
            scenario=scenario,
            user=user,
            now=datetime.now(UTC),
        )

    async def initiatives_csv(self, run_id: str, scenario: Scenario) -> bytes:
        return initiatives_csv(
            await self.results.table(run_id, scenario=scenario, include_rejected=True)
        )

    async def operations_csv(self, run_id: str) -> bytes:
        outputs = await self.results.analyses.outputs(run_id)
        fragments = await self.state.repos.fragments.list_for_document(outputs.run.doc_id)
        return operations_csv(outputs.effective.model, {f.id: f.label for f in fragments})
