"""What the stages of an analysis stored, its regenerations included (FR-083, T052).

A regeneration is a run of its own (`parent_run_id`, `process_id`): it takes the process as the
expert left it, runs S6 and S7 for it and stores the next version of its initiatives. The
analysis reads S5, its own S7 and the versions of the regenerations that succeeded; the
effective initiatives of every process are those of its latest version.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from gr.analysis.overlay import EffectiveState, apply_reviews
from gr.analysis.versions import Retired, current, merge, retired
from gr.domain.enums import RunStatus, StageName
from gr.domain.initiative import InitiativeSet, InitiativeVersion
from gr.domain.process import ProcessModel
from gr.domain.review import ReviewEvent
from gr.domain.run import AnalysisRun
from gr.storage.repositories import Repositories


@dataclass(frozen=True)
class Regeneration:
    run: AnalysisRun
    version: InitiativeVersion


@dataclass(frozen=True)
class StoredOutputs:
    model: ProcessModel | None  # S5; None until it is done
    analysis: InitiativeSet | None  # S7 of the analysis; None until it is done
    regenerations: list[Regeneration]  # those that succeeded, oldest first
    initiatives: InitiativeSet  # the latest version of every process

    @property
    def versions(self) -> list[InitiativeVersion]:
        return [r.version for r in self.regenerations]

    def version_of(self, process_id: str) -> int:
        latest = current(self.versions).get(process_id)
        return latest.version if latest else 1

    def made_at(self) -> dict[str, datetime]:
        """Regenerated process → the moment its effective initiatives took the model at."""
        return {key: found.based_on for key, found in current(self.versions).items()}

    def predecessors(self) -> dict[str, str]:
        """Initiative of a regeneration → the one it replaced, over all versions."""
        return {new: old for v in self.versions for new, old in v.predecessors.items()}

    def retired(self) -> dict[str, Retired]:
        return retired(self.analysis or InitiativeSet(), self.versions)

    def effective(self, events: Sequence[ReviewEvent]) -> EffectiveState:
        """The analysis as the expert left it (S5 must be done)."""
        if self.model is None:
            raise ValueError("the process map is not ready")
        return apply_reviews(self.model, self.initiatives, events, since=self.made_at())


async def load_outputs(repos: Repositories, run: AnalysisRun) -> StoredOutputs:
    stored_model = await repos.stage_outputs.get(run.id, StageName.VALIDATE)
    stored_result = await repos.stage_outputs.get(run.id, StageName.POSTPROCESS)
    model = ProcessModel.model_validate(stored_model) if stored_model is not None else None
    analysis = InitiativeSet.model_validate(stored_result) if stored_result is not None else None
    regenerations: list[Regeneration] = []
    for child in await repos.runs.regenerations(run):
        output = await repos.stage_outputs.get(child.id, StageName.POSTPROCESS)
        if child.status == RunStatus.SUCCEEDED and output is not None:
            regenerations.append(Regeneration(child, InitiativeVersion.model_validate(output)))
    order = [p.id for p in model.processes] if model is not None else []
    initiatives = merge(analysis or InitiativeSet(), [r.version for r in regenerations], order)
    return StoredOutputs(model, analysis, regenerations, initiatives)
