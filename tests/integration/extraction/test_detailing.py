"""S4 detail on FakeLLM (contracts/llm.md § S4, T030).

Scripted answers live in tests/fixtures/llm/detail_*.json with fragment labels in `@…@`.
"""

import re
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from gr.domain.enums import Confidence, StageName
from gr.extraction.detailing import (
    DetailedProcess,
    _ProcessText,
    detail_processes,
    merge_windows,
)
from gr.extraction.discovery import DiscoveredProcess, DiscoveryResult
from gr.extraction.render import ChunkingConfig, FragmentAliases, load_chunking
from gr.extraction.schemas import DetailOut
from gr.ingestion.txt import TxtExtractor
from gr.llm.base import LLMOutputTruncatedError, LLMTimeoutError
from gr.llm.fake import FakeCall, FakeLLM
from gr.pipeline.stages import StageError
from gr.services.parsing import ParsedDocument, ParseLimits, parse_raw

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
LIMITS = ParseLimits(max_pages=300, max_chars=1_000_000, max_unzipped_bytes=100 << 20)
_LABEL_RE = re.compile(r"@([^@]+)@")


@cache
def document() -> ParsedDocument:
    raw = TxtExtractor().extract((FIXTURES / "synthetic" / "regulation.txt").read_bytes())
    return parse_raw(raw, "doc_S", LIMITS)


def ids(*labels: str) -> list[str]:
    by_label = {f.label: f.id for f in document().fragments}
    return [by_label[label] for label in labels]


def answer(name: str) -> str:
    fragments = document().fragments
    aliases = FragmentAliases(fragments)
    by_label = {f.label: f for f in fragments}
    text = (FIXTURES / "llm" / f"detail_{name}.json").read_text("utf-8")
    return _LABEL_RE.sub(lambda m: aliases.alias(by_label[m.group(1)].id), text)


def process(key: str = "proc_1", name: str = "Выдача пропуска") -> DiscoveredProcess:
    return DiscoveredProcess(
        key=key,
        name=name,
        trigger="заявление",
        result="пропуск",
        core_fragment_ids=ids("п. 3.2", "п. 3.3", "п. 3.4"),
        related_fragment_ids=ids("п. 2.1"),
        confidence=Confidence.HIGH,
    )


def discovery(*processes: DiscoveredProcess) -> DiscoveryResult:
    return DiscoveryResult(processes=list(processes), chunks=1, prompt_version="discover@test")


class MemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    async def get(self, item_key: str) -> dict[str, Any] | None:
        return self.items.get(item_key)

    async def put(self, item_key: str, payload: dict[str, Any]) -> None:
        assert item_key not in self.items
        self.items[item_key] = payload


async def run(
    llm: FakeLLM,
    found: DiscoveryResult,
    *,
    store: MemoryStore | None = None,
    chunking: ChunkingConfig | None = None,
) -> Any:
    parsed = document()
    return await detail_processes(
        found,
        parsed.fragments,
        parsed.metadata,
        llm,
        run_id="run_T",
        max_tokens=16000,
        concurrency=2,
        store=store,
        chunking=chunking,
    )


def two_window_budget() -> ChunkingConfig:
    """The smallest budget that cuts the test process into exactly two windows."""
    fragments = document().fragments
    aliases = FragmentAliases(fragments)
    for budget in range(200, 20_000, 20):
        if len(_ProcessText(process(), fragments, aliases, budget).windows()) == 2:
            return replace(load_chunking(), detail_max_chars=budget)
    raise AssertionError("no two-window budget")


async def test_one_window_and_the_store() -> None:
    llm = FakeLLM({StageName.DETAIL: [answer("simple")]})
    store = MemoryStore()
    result = await run(llm, discovery(process()), store=store)
    (done,) = result.processes
    assert done.windows == 1 and [op.key for op in done.detail.operations] == ["o1", "o2"]
    assert result.failed == [] and result.prompt_version.startswith("detail@")
    (call,) = llm.calls
    assert call.max_tokens == 16000 and "«Выдача пропуска»" in call.user
    assert "Это часть" not in call.user
    # The related fragment (deadline) is in the text, in document order before the actions.
    assert call.user.index("десяти рабочих дней") < call.user.index("Приём и регистрация")
    assert set(store.items) == {"proc_1"}

    # A resumed run takes the stored process without calling the model.
    again = await run(FakeLLM(), discovery(process()), store=store)
    assert again.processes == result.processes


async def test_two_windows_are_merged_in_order() -> None:
    def reply(call: FakeCall) -> Any:
        return answer("window_1") if "часть 1 из 2" in call.user else answer("window_2")

    llm = FakeLLM(default=reply)
    result = await run(llm, discovery(process()), chunking=two_window_budget())
    (done,) = result.processes
    assert done.windows == 2 and len(llm.calls) == 2
    detail = done.detail
    assert [op.key for op in detail.operations] == ["w1.o1", "w1.o2", "w2.o1", "w2.o2"]
    assert detail.operations[1].depends_on == ["w1.o1"]
    assert detail.operations[2].depends_on == ["w1.o2"]  # window 2 continues window 1
    assert detail.operations[3].depends_on == ["w2.o1"]
    (point,) = detail.decision_points
    assert point.key == "w1.d1" and point.after_operation == "w1.o2"
    assert point.outcomes[0].next_operation == "o3"  # unknown in its window: left for S5
    assert len(detail.process.participants) == 1  # the same participant from both windows
    assert detail.process.result and detail.process.result[0].text == "пропуск"
    # Every window carries the card and the related deadline fragment.
    assert all("«Выдача пропуска»" in c.user and "десяти рабочих" in c.user for c in llm.calls)


async def test_truncated_window_is_halved_and_succeeds() -> None:
    llm = FakeLLM(
        {
            StageName.DETAIL: [
                LLMOutputTruncatedError(),
                answer("window_1"),
                answer("window_2"),
            ]
        }
    )
    result = await run(llm, discovery(process()))
    (done,) = result.processes
    assert done.windows == 2 and len(llm.calls) == 3
    first, second = llm.calls[1].user, llm.calls[2].user
    assert "Приём и регистрация" in first and "Приём и регистрация" not in second
    assert done.detail.operations[0].key == "w1.o1"


async def test_truncation_that_cannot_be_split_fails_the_process() -> None:
    single = process(name="Один пункт").model_copy(update={"core_fragment_ids": ids("п. 3.2.1")})

    def reply(call: FakeCall) -> Any:
        return LLMOutputTruncatedError() if "«Один пункт»" in call.user else answer("simple")

    llm = FakeLLM(default=reply)
    result = await run(llm, discovery(single, *(process(f"proc_{n}") for n in (2, 3, 4, 5))))
    assert [f.process_key for f in result.failed] == ["proc_1"]
    assert result.failed[0].code == "llm_output_truncated"


def _failing(names: set[str]) -> FakeLLM:
    def reply(call: FakeCall) -> Any:
        if any(f"«{name}»" in call.user for name in names):
            return LLMTimeoutError()
        return answer("simple")

    return FakeLLM(default=reply)


async def test_one_of_five_failing_is_reported_not_fatal() -> None:
    found = discovery(*(process(f"proc_{n}", f"Процесс {n}") for n in range(1, 6)))
    result = await run(_failing({"Процесс 3"}), found)
    assert [p.process_key for p in result.processes] == ["proc_1", "proc_2", "proc_4", "proc_5"]
    (failure,) = result.failed
    assert (failure.process_key, failure.code) == ("proc_3", "llm_timeout")
    assert failure.name == "Процесс 3"


async def test_three_of_five_failing_fails_the_stage() -> None:
    found = discovery(*(process(f"proc_{n}", f"Процесс {n}") for n in range(1, 6)))
    with pytest.raises(StageError) as error:
        await run(_failing({"Процесс 1", "Процесс 2", "Процесс 4"}), found)
    assert error.value.code == "llm_timeout" and "3 из 5" in error.value.message_ru


async def test_no_processes_no_calls() -> None:
    llm = FakeLLM()
    result = await run(llm, discovery())
    assert result.processes == [] and llm.calls == []


def test_merge_of_a_single_window_is_identity() -> None:
    detail = DetailOut.model_validate_json(answer("simple"))
    assert merge_windows([detail]) is detail
    assert DetailedProcess(process_key="p", windows=1, detail=detail).detail.operations
