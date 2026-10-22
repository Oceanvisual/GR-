"""Builders of S3/S4 answers over the synthetic regulation for the validation tests."""

from functools import cache
from pathlib import Path
from typing import Any

from gr.domain.document import Fragment
from gr.domain.enums import Confidence
from gr.domain.process import ProcessModel
from gr.extraction.detailing import DetailedProcess, DetailingResult
from gr.extraction.discovery import DiscoveredProcess, DiscoveryResult
from gr.extraction.render import FragmentAliases
from gr.extraction.schemas import DetailOut
from gr.ingestion.txt import TxtExtractor
from gr.services.parsing import ParseLimits, parse_raw
from gr.validation.process_validator import validate

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
LIMITS = ParseLimits(max_pages=300, max_chars=1_000_000, max_unzipped_bytes=100 << 20)


@cache
def _fragments() -> tuple[Fragment, ...]:
    raw = TxtExtractor().extract((FIXTURES / "synthetic" / "regulation.txt").read_bytes())
    return tuple(parse_raw(raw, "doc_V", LIMITS).fragments)


def fragments(**changes_by_label: dict[str, Any]) -> list[Fragment]:
    """The synthetic fragments, some changed: fragments(**{"п. 3.2.1": {"repealed": True}})."""
    return [f.model_copy(update=changes_by_label.get(f.label, {})) for f in _fragments()]


def fid(label: str) -> str:
    return next(f.id for f in _fragments() if f.label == label)


def alias(label: str) -> str:
    return FragmentAliases(list(_fragments())).alias(fid(label))


def fact(label: str, quote: str, text: str | None = None) -> dict[str, str]:
    return {"text": text or quote, "ref": alias(label), "quote": quote}


def op(key: str, label: str, quote: str, **fields: Any) -> dict[str, Any]:
    return {"key": key, "name": quote[:40], "action": fact(label, quote), **fields}


def detail(
    operations: list[dict[str, Any]],
    decision_points: list[dict[str, Any]] | None = None,
    **process: Any,
) -> DetailOut:
    head = {"name": fact("п. 3.2", "Приём и регистрация заявления"), **process}
    return DetailOut.model_validate(
        {"process": head, "operations": operations, "decision_points": decision_points or []}
    )


def discovered(
    key: str = "proc_1", name: str = "Приём заявления", **fields: Any
) -> DiscoveredProcess:
    values: dict[str, Any] = {
        "key": key,
        "name": name,
        "core_fragment_ids": [fid("п. 3.2")],
        "related_fragment_ids": [],
        "confidence": Confidence.HIGH,
        **fields,
    }
    return DiscoveredProcess.model_validate(values)


def run(
    *details: DetailOut,
    found: list[DiscoveredProcess] | None = None,
    frags: list[Fragment] | None = None,
    non_process: list[str] | None = None,
) -> ProcessModel:
    processes = found or [
        discovered(f"proc_{n}", f"Процесс {n}") for n in range(1, len(details) + 1)
    ]
    discovery = DiscoveryResult(
        processes=processes,
        non_process_fragment_ids=non_process or [],
        chunks=1,
        prompt_version="discover@test",
    )
    detailing = DetailingResult(
        processes=[
            DetailedProcess(process_key=p.key, windows=1, detail=d)
            for p, d in zip(processes, details, strict=True)
        ],
        prompt_version="detail@test",
    )
    return validate(discovery, detailing, frags or fragments())
