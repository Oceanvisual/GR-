"""Spike T074, step 5: the same process with a schema that has no nullable fields.

Optional object → list of 0..1, optional string → "", optional number → -1.
Usage: uv run python spikes/llm/probe_detail_strict.py <regulation.docx> <first heading> <next heading>
"""

import asyncio
import sys
import time
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
import probe_detail as base

from gr.domain.enums import StageName
from gr.llm.openai_compat import OpenAICompatibleLLM
from gr.settings import Settings

GroundedStr = base.GroundedStr


class DeadlineOut(BaseModel):
    raw: GroundedStr
    value: int  # -1 если числа нет
    unit: base.Unit  # type: ignore[valid-type]
    anchor: str  # "" если не указано


class InputOut(BaseModel):
    name: GroundedStr
    mandatory: bool


class ProcessOut(BaseModel):
    name: GroundedStr
    goal: list[GroundedStr]
    trigger: list[GroundedStr]
    result: list[GroundedStr]
    participants: list[GroundedStr]
    inputs: list[InputOut]
    information_systems: list[GroundedStr]
    deadlines: list[DeadlineOut]


class OperationOut(BaseModel):
    key: str
    name: str
    action: GroundedStr
    object: str
    performer: list[GroundedStr]
    inputs: list[GroundedStr]
    outputs: list[GroundedStr]
    depends_on: list[str]
    deadline: list[DeadlineOut]
    conditions: list[GroundedStr]
    information_systems: list[GroundedStr]
    operation_type: base.OpType  # type: ignore[valid-type]
    legally_significant: bool
    handles_personal_data: bool
    is_loop_back: bool


class OutcomeOut(BaseModel):
    label: str
    next_operation: str  # "" если процесс завершается
    ends_process: bool


class DecisionOut(BaseModel):
    key: str
    after_operation: str
    condition: GroundedStr
    outcomes: list[OutcomeOut]
    criteria: list[GroundedStr]


class DetailOut(BaseModel):
    process: ProcessOut
    operations: list[OperationOut]
    decision_points: list[DecisionOut]


SYSTEM = (
    base.SYSTEM
    + """
Необязательные поля: объект, которого нет в документе, — пустой список; строка — "";
число — -1. Не более одного элемента в goal, trigger, result, performer, deadline."""
)


async def main(path: str, first: str, stop: str) -> None:
    settings = Settings()
    frags = base.fragments(path, first, stop)
    by_id = dict(frags)
    user = (
        "<task>Извлеки процесс, описанный в подразделе, до уровня операций.</task>\n<document>\n"
        + "\n".join(f'[fragment id="{fid}"]\n{text}\n[/fragment]' for fid, text in frags)
        + "\n</document>"
    )
    llm = OpenAICompatibleLLM(settings)
    started = time.perf_counter()
    result = await llm.generate(
        schema=DetailOut,
        system=SYSTEM,
        user=user,
        stage=StageName.DETAIL,
        max_tokens=settings.llm_max_output_tokens,
        run_id="spike-detail-strict",
    )
    out = result.value
    print(
        f"mode={llm._mode} attempts={result.attempts} in={result.input_tokens} out={result.output_tokens} "
        f"wall={time.perf_counter() - started:.1f}s cost≈${result.input_tokens * 2e-6 + result.output_tokens * 1e-5:.3f}"
    )
    statuses: dict[str, int] = {}
    bad = 0
    for item in base.grounded(out):
        if item.fragment_id not in by_id:
            bad += 1
            continue
        status = base.check_quote(item.quote, by_id[item.fragment_id])
        statuses[status] = statuses.get(status, 0) + 1
    print(f"grounded: {sum(statuses.values()) + bad}; {statuses}; unknown aliases: {bad}")
    print(f"operations: {len(out.operations)}, decision points: {len(out.decision_points)}")
    for op in out.operations:
        deadline = f"{op.deadline[0].value} {op.deadline[0].unit}" if op.deadline else "—"
        performer = op.performer[0].value[:40] if op.performer else "—"
        print(f"   {op.key} [{op.operation_type}] {op.name[:55]} | {performer} | {deadline}")
    for dp in out.decision_points:
        print(
            f"   {dp.key} after {dp.after_operation}: {[(o.label[:40], o.next_operation, o.ends_process) for o in dp.outcomes]}"
        )


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:4]))
