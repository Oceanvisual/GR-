"""Offline S4 → S5 eval from saved discovery and exact recorded request hashes (T036).

No settings, env files or network client are loaded. A missing request stops the replay.
The original database is opened read-only. Output is separate from live runs and baseline.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from evals.golden import Case, load_cases
from evals.metrics import DocumentScore, score_document
from evals.review import review_markdown
from evals.target import ROOT, Outcome, to_prediction
from gr.domain.document import Document, Fragment
from gr.domain.enums import StageName
from gr.domain.run import AnalysisRun
from gr.extraction.detailing import detail_processes
from gr.extraction.discovery import DiscoveryResult
from gr.extraction.prompt_files import load_prompt
from gr.extraction.render import load_chunking
from gr.llm.base import LLMInvalidOutputError, LLMOutputTruncatedError, LLMResult
from gr.llm.cache import request_hash
from gr.llm.openai_compat import unwrap_json_fence
from gr.llm.options import request_options
from gr.validation.process_validator import validate


class ReplayError(ValueError):
    pass


class RecordedCall(BaseModel):
    model: str
    request_hash: str
    response: dict[str, Any] | None


class SourceEntry(BaseModel):
    outcome: Outcome | None
    error: str | None = None


class SourceReport(BaseModel):
    documents: dict[str, SourceEntry]


class ReplayLLM:
    """Recorded responses in attempt order, matched against the complete request hash."""

    def __init__(
        self, calls: list[RecordedCall], options_for: Callable[[str], dict[str, Any]]
    ) -> None:
        self.calls = calls
        self.options_for = options_for  # request options of the model of a recorded call
        self.requests = 0

    async def generate[T: BaseModel](
        self,
        *,
        schema: type[T],
        system: str,
        user: str,
        stage: StageName,
        max_tokens: int,
        run_id: str,
    ) -> LLMResult[T]:
        self.requests += 1
        keys = {
            request_hash(c.model, schema, system, user, self.options_for(c.model))
            for c in self.calls
        }
        matches = [c for c in self.calls if c.request_hash in keys]
        if not matches:
            raise ReplayError("Нет точного request_hash: изменились входные данные или настройки.")
        for call in matches[:2]:  # same repair limit as the live client
            raw = call.response or {}
            choices = raw.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason") == "length":
                raise LLMOutputTruncatedError()
            content = choice.get("message", {}).get("content") or ""
            try:
                value = schema.model_validate_json(unwrap_json_fence(content))
            except ValidationError:
                continue
            return LLMResult(
                value=value,
                model=call.model,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                attempts=0,
                cached=True,
            )
        raise LLMInvalidOutputError()


class Snapshot(BaseModel):
    run: AnalysisRun
    document: Document
    fragments: list[Fragment]
    full_text: str
    discovery: DiscoveryResult
    calls: list[RecordedCall]


def read_snapshot(database: Path, run_id: str) -> Snapshot:
    """Read one consistent snapshot; mode=ro also prevents creating a missing database."""
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")

        def payload(query: str, key: str) -> str:
            row = conn.execute(query, (key,)).fetchone()
            if row is None:
                raise ReplayError(f"Нет сохранённых данных для {key}.")
            return str(row[0])

        run = AnalysisRun.model_validate_json(
            payload("SELECT payload FROM analysis_runs WHERE id=?", run_id)
        )
        return Snapshot(
            run=run,
            document=Document.model_validate_json(
                payload("SELECT payload FROM documents WHERE id=?", run.doc_id)
            ),
            fragments=[
                Fragment.model_validate_json(row[0])
                for row in conn.execute(
                    'SELECT payload FROM fragments WHERE doc_id=? ORDER BY "order"', (run.doc_id,)
                )
            ],
            full_text=payload("SELECT full_text FROM document_texts WHERE doc_id=?", run.doc_id),
            discovery=DiscoveryResult.model_validate_json(
                payload(
                    "SELECT payload FROM stage_outputs WHERE stage='discover' AND run_id=?", run_id
                )
            ),
            calls=[
                RecordedCall(
                    model=model, request_hash=key, response=json.loads(raw) if raw else None
                )
                for model, key, raw in conn.execute(
                    "SELECT model,request_hash,response FROM llm_calls "
                    "WHERE run_id=? AND stage='detail' ORDER BY created_at,attempt,id",
                    (run_id,),
                )
            ],
        )


async def replay_case(
    case: Case, snapshot: Snapshot, *, provider_host: str | None
) -> tuple[Outcome, DocumentScore]:
    golden = case.load_golden()
    if golden.sha256 != snapshot.document.sha256:
        raise ReplayError(f"{case.id}: эталон относится к другой редакции документа.")
    config = snapshot.run.config_snapshot
    if config.chunking != vars(load_chunking()):
        raise ReplayError(f"{case.id}: изменились настройки разбиения.")
    prompt = load_prompt("detail")
    if config.prompt_versions.get("detail") != prompt.version:
        raise ReplayError(f"{case.id}: изменилась версия промпта детализации.")
    llm = ReplayLLM(
        snapshot.calls, lambda model: request_options(StageName.DETAIL, provider_host, model)
    )
    detail = await detail_processes(
        snapshot.discovery,
        snapshot.fragments,
        snapshot.document.metadata,
        llm,
        run_id=snapshot.run.id,
        max_tokens=16000,
        concurrency=1,
    )
    model = validate(snapshot.discovery, detail, snapshot.fragments)
    prediction = to_prediction(model, snapshot.fragments, snapshot.full_text)
    outcome = Outcome(
        prediction=prediction,
        run_id=snapshot.run.id,
        status="succeeded",
        complete=not detail.failed,
        failed_processes=len(detail.failed),
        usage={"calls": 0, "in_tokens": 0, "out_tokens": 0, "latency_s": 0, "cost": 0},
        models=config.models,
        prompt_versions=config.prompt_versions,
    )
    return outcome, score_document(golden, prediction)


async def replay_report(
    source: Path, database: Path, *, provider_host: str | None, only: list[str] | None
) -> dict[str, Any]:
    report = SourceReport.model_validate_json(await asyncio.to_thread(source.read_text, "utf-8"))
    cases = await asyncio.to_thread(load_cases)
    if only:
        unknown = set(only) - {c.id for c in cases}
        if unknown:
            raise ReplayError(f"Неизвестные документы: {', '.join(sorted(unknown))}.")
        cases = [c for c in cases if c.id in only]
    documents: dict[str, Any] = {}
    for case in cases:
        entry = report.documents.get(case.id)
        if entry is None or entry.outcome is None:
            raise ReplayError(f"{case.id}: в исходном отчёте нет результата анализа.")
        snapshot = await asyncio.to_thread(read_snapshot, database, entry.outcome.run_id)
        outcome, score = await replay_case(case, snapshot, provider_host=provider_host)
        documents[case.id] = {
            "error": None,
            "outcome": outcome.model_dump(mode="json"),
            "score": score.model_dump(mode="json"),
            "golden_verified_by": case.load_golden().verified_by,
            "expert_review": review_markdown(
                case.load_golden(),
                snapshot.fragments,
                snapshot.discovery,
                outcome.prediction,
                score,
            ),
        }
    return {
        "mode": "offline_replay",
        "source": str(source),
        "cost_usd": 0,
        "saved_stages": ["discover"],
        "replayed_stages": ["detail", "validate"],
        "provider_host_for_request_options": provider_host,
        "documents": documents,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="сохранённый отчёт eval")
    parser.add_argument("--database", type=Path, default=ROOT / "evals/.data/gr.db")
    parser.add_argument("--provider-host", help="хост исходного провайдера для параметров запроса")
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--output", type=Path, required=True, help="отдельный файл отчёта")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() in {args.source.resolve(), args.database.resolve()}:
            raise ReplayError("Результат должен сохраняться отдельно от исходных данных.")
        if args.output.resolve() == (ROOT / "evals/baseline.json").resolve():
            raise ReplayError("Офлайн-прогон не может перезаписать baseline.")
        result = asyncio.run(
            replay_report(
                args.source,
                args.database,
                provider_host=args.provider_host,
                only=args.only,
            )
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
        for n, entry in enumerate(result["documents"].values(), 1):
            review = entry.get("expert_review")
            if review:
                path = args.output.with_name(f"{args.output.stem}-review-{n}.md")
                if path.resolve() in {args.source.resolve(), args.database.resolve()}:
                    raise ReplayError("Путь экспертной сверки совпадает с исходными данными.")
                path.write_text(review, "utf-8")
    except (ReplayError, OSError, sqlite3.Error, ValidationError) as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 2
    print(f"Офлайн-прогон: {args.output}; стоимость $0")
    return 1 if any(not d["outcome"]["complete"] for d in result["documents"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
