"""S3 discover on FakeLLM (contracts/llm.md § S3, T029).

Scripted answers live in tests/fixtures/llm/discover_*.json and name fragments by label in
`@…@`; the test replaces them with the aliases of the synthetic regulation, so the fixtures
do not depend on alias numbering.
"""

import json
import re
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from gr.domain.document import Fragment
from gr.domain.enums import Confidence, GroundingStatus, StageName
from gr.extraction.discovery import MAX_TOKENS, DiscoveryResult, discover
from gr.extraction.render import FragmentAliases, load_chunking
from gr.ingestion.txt import TxtExtractor
from gr.llm.base import LLMTimeoutError
from gr.llm.fake import FakeCall, FakeLLM
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
    """A fixture with `@label@` replaced by the alias of that fragment."""
    fragments = document().fragments
    aliases = FragmentAliases(fragments)
    by_label = {f.label: f for f in fragments}
    text = (FIXTURES / "llm" / f"discover_{name}.json").read_text("utf-8")
    return _LABEL_RE.sub(lambda m: aliases.alias(by_label[m.group(1)].id), text)


async def run(llm: FakeLLM, *, budget: int | None = None) -> DiscoveryResult:
    parsed = document()
    chunking = replace(load_chunking(), outline_max_chars=budget) if budget else None
    return await discover(parsed.fragments, parsed.metadata, llm, run_id="run_T", chunking=chunking)


def codes(result: DiscoveryResult) -> list[str]:
    return [issue.code for issue in result.issues]


async def test_correct_answer() -> None:
    llm = FakeLLM({StageName.DISCOVER: [answer("correct")]})
    result = await run(llm)
    (process,) = result.processes
    assert process.key == "proc_1" and process.name.startswith("Выдача пропуска")
    assert process.core_fragment_ids == ids("п. 3.2", "п. 3.3", "п. 3.4")
    assert process.related_fragment_ids == ids("п. 2.1", "п. 2.2", "п. 2.3")  # core removed
    assert process.evidence is not None
    assert process.evidence.grounding.status == GroundingStatus.VERIFIED
    assert process.confidence == Confidence.HIGH
    assert result.non_process_fragment_ids == ids("разд. I", "п. 4.1", "п. 4.2", "прил. 1")
    assert result.issues == [] and result.chunks == 1
    assert result.prompt_version.startswith("discover@")

    (call,) = llm.calls
    assert call.max_tokens == MAX_TOKENS == 8000 and call.run_id == "run_T"
    assert call.system.startswith("Общие правила") and "<document>" in call.user
    assert "Это часть" not in call.user


async def test_unknown_ids_are_dropped_with_issues() -> None:
    result = await run(FakeLLM({StageName.DISCOVER: [answer("unknown_ids")]}))
    (process,) = result.processes  # p2 had no existing action fragments
    assert process.core_fragment_ids == ids("п. 3.2")
    assert process.related_fragment_ids == ids("п. 2.1")
    issues = codes(result)
    assert issues.count("unknown_fragment") == 6  # F999, «п. 3.3», F0, F500, F501, F777
    assert "empty_core" in issues
    # The evidence was cited from the wrong point: relocated, and the check passes.
    assert process.evidence is not None
    assert process.evidence.grounding.status == GroundingStatus.RELOCATED
    assert process.evidence.fragment_id == ids("п. 3.4.1")[0]
    assert process.confidence == Confidence.MEDIUM  # relocated: medium (R-11)
    assert result.non_process_fragment_ids == []


async def test_duplicates_from_two_chunks_are_merged() -> None:
    replies = iter([answer("chunk_a"), answer("chunk_b")])

    def reply(call: FakeCall) -> Any:
        return next(replies, answer("empty"))  # the remaining chunks find nothing

    llm = FakeLLM(default=reply)
    result = await run(llm, budget=1500)
    assert result.chunks >= 2 and len(llm.calls) == result.chunks
    assert all("Это часть" in call.user for call in llm.calls)
    merged = result.processes[0]
    assert merged.name == "Выдача пропуска"  # the first one wins
    assert merged.result == "пропуск"  # gaps are filled from the duplicate
    assert merged.core_fragment_ids == ids("п. 3.2", "п. 3.3", "п. 3.4")
    assert "duplicate_merged" in codes(result)
    checks = result.processes[1]
    assert checks.core_fragment_ids == ids("п. 4.2")
    assert checks.parent_key == merged.key  # «p1» of chunk b was merged into proc_1


async def test_empty_answer_means_no_processes() -> None:
    result = await run(FakeLLM({StageName.DISCOVER: [answer("empty")]}))
    assert result.processes == [] and codes(result) == ["no_processes"]
    assert result.notes == ["акт не содержит процедур"]


async def test_hallucinated_evidence_gives_low_confidence() -> None:
    result = await run(FakeLLM({StageName.DISCOVER: [answer("hallucinated")]}))
    (process,) = result.processes
    assert process.confidence == Confidence.LOW
    assert process.evidence is not None and not process.evidence.grounding.status.passes
    assert "unverified_evidence" in codes(result)


async def test_llm_error_propagates_to_the_stage() -> None:
    with pytest.raises(LLMTimeoutError):
        await run(FakeLLM({StageName.DISCOVER: [LLMTimeoutError()]}))


async def test_processes_are_ordered_by_document_position() -> None:
    late_first = answer("correct").replace('"key": "p1"', '"key": "p9"')
    fragments: list[Fragment] = document().fragments
    aliases = FragmentAliases(fragments)
    alias = aliases.alias(ids("п. 1.1")[0])
    early = (
        '{"key": "p1", "name": "Ранний", "core_refs": ["' + alias + '"], "evidence": '
        '{"text": "x", "ref": "' + alias + '", "quote": "устанавливает порядок и сроки выдачи"}}'
    )
    both = late_first.replace('"processes": [', '"processes": [' + early + ",", 1)
    result = await run(FakeLLM({StageName.DISCOVER: [both]}))
    assert result.processes[0].name == "Ранний"
    assert [p.key for p in result.processes] == ["proc_1", "proc_2"]


async def test_nested_references_collapse_to_the_topmost() -> None:
    fragments = document().fragments
    aliases = FragmentAliases(fragments)
    a = {label: aliases.alias(i) for label, i in zip(
        ("п. 3.2", "п. 3.2.1", "п. 3.2.2", "п. 2.1", "разд. IV", "п. 4.1"),
        ids("п. 3.2", "п. 3.2.1", "п. 3.2.2", "п. 2.1", "разд. IV", "п. 4.1"),
        strict=True,
    )}  # fmt: skip
    reply = {
        "processes": [
            {
                "key": "p1",
                "name": "Приём заявления",
                "core_refs": [a["п. 3.2.1"], a["п. 3.2"]],
                "related_refs": [a["п. 3.2.2"], a["п. 2.1"]],
                "evidence": {"text": "x", "ref": a["п. 3.2.1"], "quote": "принимает заявление"},
            }
        ],
        "non_process_refs": [a["п. 4.1"], a["разд. IV"]],
    }
    result = await run(FakeLLM({StageName.DISCOVER: [reply]}))
    (process,) = result.processes
    assert process.core_fragment_ids == ids("п. 3.2")
    assert process.related_fragment_ids == ids("п. 2.1")  # п. 3.2.2 is inside the core
    assert result.non_process_fragment_ids == ids("разд. IV")


async def test_one_procedure_named_with_two_channels_is_one_process() -> None:
    # T059: the model listed the same procedure for paper and for the electronic form.
    aliases = FragmentAliases(document().fragments)
    alias = {f.label: aliases.alias(f.id) for f in document().fragments if f.parent_id}

    def process(key: str, name: str, label: str) -> dict[str, Any]:
        return {"key": key, "name": name, "trigger": "", "result": "",
                "core_refs": [alias[label]], "related_refs": [],
                "evidence": {"text": "приём", "ref": alias[label], "quote": ""},
                "parent_key": ""}  # fmt: skip

    reply = json.dumps({
        "processes": [
            process("p1", "Приём и регистрация заявления (личное обращение)", "п. 3.2"),
            process("p2", "Приём и регистрация заявления в электронной форме", "п. 3.4"),
            process("p3", "Рассмотрение заявления", "п. 3.3"),
        ],
        "non_process_refs": [], "notes": "",
    }, ensure_ascii=False)  # fmt: skip
    result = await run(FakeLLM({StageName.DISCOVER: [reply]}))
    names = [p.name for p in result.processes]
    assert names == ["Приём и регистрация заявления (личное обращение)", "Рассмотрение заявления"]
    assert result.processes[0].core_fragment_ids == ids("п. 3.2", "п. 3.4")
    assert "duplicate_merged" in codes(result)


async def test_a_fragment_holding_a_process_is_not_outside_processes() -> None:
    # reg261 (T062): «body» answered as non-process took the whole act out of coverage.
    fragments = document().fragments
    aliases = FragmentAliases(fragments)
    a = {label: aliases.alias(i) for label, i in zip(
        ("п. 3.2", "разд. III", "разд. IV"), ids("п. 3.2", "разд. III", "разд. IV"), strict=True
    )}  # fmt: skip
    reply = {
        "processes": [
            {
                "key": "p1",
                "name": "Приём заявления",
                "core_refs": [a["п. 3.2"]],
                "evidence": {"text": "x", "ref": a["п. 3.2"], "quote": "принимает заявление"},
            }
        ],
        "non_process_refs": [a["разд. III"], a["разд. IV"]],
    }
    result = await run(FakeLLM({StageName.DISCOVER: [reply]}))
    assert result.non_process_fragment_ids == ids("разд. IV")
