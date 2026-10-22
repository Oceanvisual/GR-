"""JSON export of an analysis (FR-092, FR-093, api.md § Экспорт, T054).

`AnalysisExport` is the full result as the screens show it: the document and its structure,
the run with its configuration and usage, the process model and the initiatives as the expert
left them (the effective version of every process), the assumptions and the priority
configuration everything is computed from, the economics of the three scenarios, the
priorities, the whole review log and the disclaimers. Every element names its knowledge layer,
and every element an expert reviews carries its review status. The published JSON Schema is
generated from the model, so it cannot drift from what is exported.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from gr.analysis.overlay import EffectiveState
from gr.domain.assumptions import AssumptionSet
from gr.domain.base import DomainModel
from gr.domain.document import Document, Fragment
from gr.domain.economics import EconomicsResult
from gr.domain.enums import Scenario
from gr.domain.initiative import Finding, Initiative, NoChangeDecision
from gr.domain.priority import PriorityConfig, PriorityConfigVersion, PriorityResult
from gr.domain.process import CoverageReport, Process, UnverifiedItem
from gr.domain.review import ReviewEvent
from gr.domain.run import AnalysisRun

SCHEMA_VERSION: Literal["1.0"] = "1.0"


class ScenarioEconomics(DomainModel):
    """The economics of the three scenarios; the portfolio is the same in all (FR-061)."""

    conservative: EconomicsResult
    base: EconomicsResult
    optimistic: EconomicsResult


class AnalysisExport(DomainModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    exported_at: datetime
    exported_by: str
    scenario: Scenario  # of the disclaimers; the economics hold all three scenarios
    document: Document
    structure: list[Fragment]
    run: AnalysisRun
    processes: list[Process]
    coverage: CoverageReport
    unverified: list[UnverifiedItem]
    findings: list[Finding]
    initiatives: list[Initiative]
    no_change: list[NoChangeDecision]
    excluded: dict[str, str]  # initiative → why it is out of the calculations
    initiative_versions: dict[str, int]  # process → the version of its initiatives (FR-083)
    assumptions: AssumptionSet
    priority_config: PriorityConfig
    priority_config_version: int
    economics: ScenarioEconomics
    priority: list[PriorityResult]
    reviews: list[ReviewEvent]  # the whole log, in the order the decisions were made
    disclaimers: list[str]


def build_export(
    *,
    document: Document,
    fragments: Sequence[Fragment],
    run: AnalysisRun,
    effective: EffectiveState,
    versions: Mapping[str, int],
    assumptions: AssumptionSet,
    config: PriorityConfigVersion,
    economics: Mapping[Scenario, EconomicsResult],
    priorities: Sequence[PriorityResult],
    reviews: Sequence[ReviewEvent],
    disclaimers: Sequence[str],
    scenario: Scenario,
    user: str,
    now: datetime,
) -> AnalysisExport:
    model = effective.model
    return AnalysisExport(
        exported_at=now,
        exported_by=user,
        scenario=scenario,
        document=document,
        structure=sorted(fragments, key=lambda f: f.order),
        run=run,
        processes=model.processes,
        coverage=model.coverage,
        unverified=model.unverified,
        findings=effective.findings,
        initiatives=effective.initiatives,
        no_change=effective.result.no_change,
        excluded=dict(effective.excluded),
        initiative_versions={p.id: versions.get(p.id, 1) for p in model.processes},
        assumptions=assumptions,
        priority_config=config.config,
        priority_config_version=config.version,
        economics=ScenarioEconomics(**{s.value: economics[s] for s in Scenario}),
        priority=sorted(priorities, key=lambda p: p.initiative_id),
        reviews=sorted(reviews, key=lambda e: (e.created_at, e.id)),
        disclaimers=list(disclaimers),
    )


def export_schema() -> dict[str, Any]:
    """JSON Schema of `AnalysisExport` as it is written (api.md: /schema/analysis-export.json)."""
    schema = AnalysisExport.model_json_schema(mode="serialization")
    return {"$schema": "https://json-schema.org/draft/2020-12/schema"} | schema
