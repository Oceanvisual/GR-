"""Eval of process extraction: dataset.jsonl → pipeline on live LLM → metrics → gate.

    uv run python -m evals.run                     # all documents, gate against baseline.json
    uv run python -m evals.run --only reg261       # one document, no gate
    uv run python -m evals.run --cached            # cache hits reused; misses call the LLM
    uv run python -m evals.run --rescore evals/runs/<stamp>.json   # metrics again, no LLM
    uv run python -m evals.run --update-baseline   # record this run as the baseline
    uv run python -m evals.run --strict            # also the targets of SC-003 / SC-002
    uv run python -m evals.run --s6                # with S6: the initiatives (T066)
    uv run python -m evals.run --judge             # S6 and the LLM judge, verdicts cached
    uv run python -m evals.run --budget-usd 2      # live run under a cost limit ([live_budget])
    uv run python -m evals.run --discovery-model anthropic/claude-sonnet-5.5  # S3 on its own model

The model comes from the environment (LLM_MODEL_EXTRACTION and the rest), as for the service.
Exit: 0 — fine, 1 — regression, ceiling, failed target or a document not analysed,
2 — the dataset or the golden annotation does not match the documents.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import tomllib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from evals.budget import Budget
from evals.golden import EVAL_DIR, Case, Golden, load_cases
from evals.judge import Judge
from evals.metrics import (
    USAGE,
    DocumentScore,
    aggregate,
    gate,
    score_document,
)
from evals.metrics_opportunities import score_opportunities
from evals.target import ROOT, EvalRunError, Outcome, analyse, judge_client, pipeline

RUNS_DIR = EVAL_DIR / "runs"
BASELINE = EVAL_DIR / "baseline.json"


class ConfigError(Exception):
    pass


def load_config() -> dict[str, Any]:
    return tomllib.loads((EVAL_DIR / "config.toml").read_text("utf-8"))


def checked_golden(case: Case) -> Golden:
    golden = case.load_golden()
    digest = hashlib.sha256((ROOT / case.path).read_bytes()).hexdigest()
    if digest != golden.sha256:
        raise ConfigError(f"{case.id}: эталон размечен по другой редакции файла {case.path}")
    return golden


async def run_documents(
    cases: list[Case],
    *,
    cached: bool,
    budget: Budget | None = None,
    s6: bool = False,
    discovery_model: str | None = None,
) -> dict[str, Outcome | str]:
    """Outcome per document, or the reason it could not be analysed."""
    results: dict[str, Outcome | str] = {}
    async with pipeline(
        cached=cached, budget=budget, s6=s6, discovery_model=discovery_model
    ) as state:
        for case in cases:
            print(f"… {case.id}: анализ", flush=True)
            try:
                results[case.id] = await analyse(state, ROOT / case.path, s6=s6)
            except EvalRunError as exc:
                results[case.id] = str(exc)
    return results


async def judge_documents(
    outcomes: dict[str, Outcome | str], model: str, budget: Budget | None = None
) -> dict[str, dict[str, float | None]]:
    """The judge's metrics per document with S6 results; new verdicts go to the cache."""
    found: dict[str, dict[str, float | None]] = {}
    async with judge_client(model, budget) as llm:
        judge = Judge(llm, model)
        try:
            for doc_id, outcome in outcomes.items():
                if isinstance(outcome, str) or outcome.opportunities is None:
                    continue
                print(f"… {doc_id}: судья S6", flush=True)
                found[doc_id] = await judge.score(outcome.opportunities.initiatives)
        finally:
            judge.save()  # verdicts paid for are kept even if a later call fails
    print(f"судья: новых вызовов {judge.calls}")
    return found


def stored_outcomes(path: Path, cases: list[Case]) -> dict[str, Outcome | str]:
    stored = json.loads(path.read_text("utf-8"))["documents"]
    results: dict[str, Outcome | str] = {}
    for case in cases:
        entry = stored.get(case.id)
        if entry is None:
            raise ConfigError(f"в {path.name} нет документа {case.id}")
        results[case.id] = entry["error"] or Outcome.model_validate(entry["outcome"])
    return results


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def print_documents(scores: dict[str, DocumentScore], outcomes: dict[str, Outcome | str]) -> None:
    print(
        "\n| документ | процессы: полнота | точность | операции | сроки | verified "
        "| сроки не из текста | процессов | токены вх/вых | время, с |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for doc_id, outcome in outcomes.items():
        if isinstance(outcome, str):
            print(f"| {doc_id} | ❌ {outcome} |||||||||")
            continue
        m = scores[doc_id].metrics
        u = outcome.usage
        print(
            f"| {doc_id} | {_fmt(m['process_recall'])} | {_fmt(m['process_precision'])} "
            f"| {_fmt(m['operation_recall'])} | {_fmt(m['deadline_recall'])} "
            f"| {_fmt(m['verified_share'])} | {_fmt(m['deadlines_not_in_text'], 0)} "
            f"| {_fmt(m['processes_predicted'], 0)} | {u['in_tokens']:.0f}/{u['out_tokens']:.0f} "
            f"| {u['latency_s']:.0f} |"
        )
    for doc_id, score in scores.items():
        lines = [f"  не найден: {name}" for name in score.missed]
        lines += [f"  лишний: {name}" for name in score.extra]
        lines += [
            f"  {m.golden} ← «{m.predicted}»: операции {m.operations_found}/{m.operations_total}"
            + (f"; пропущены: {'; '.join(m.missed_operations)}" if m.missed_operations else "")
            for m in score.matches
        ]
        lines += [f"  срок не из текста: {d}" for d in score.bad_deadlines]
        if lines:
            print(f"\n{doc_id}:\n" + "\n".join(lines))


def print_s6(per_document: dict[str, dict[str, float | None]]) -> None:
    rows = {d: m for d, m in per_document.items() if m.get("s6_initiatives") is not None}
    if not rows:
        return
    print(
        "\n| документ | S6: инициатив | ссылки целы | LLM/агент для простых | есть проще "
        "| no_change | судья: обосновано | конкретно |\n|---|---|---|---|---|---|---|---|"
    )
    for doc_id, m in rows.items():
        print(
            f"| {doc_id} | {_fmt(m['s6_initiatives'], 0)} | {_fmt(m['s6_reference_integrity'])} "
            f"| {_fmt(m['s6_llm_for_classic_share'])} | {_fmt(m['s6_simpler_alternative_share'])} "
            f"| {_fmt(m['s6_no_change_share'])} | {_fmt(m.get('s6_judge_grounded'))} "
            f"| {_fmt(m.get('s6_judge_concrete'))} |"
        )


def _main(args: argparse.Namespace) -> int:
    try:
        if args.accept_regressions and not args.update_baseline:
            raise ConfigError("--accept-regressions работает только с --update-baseline")
        if args.update_baseline and (args.only or args.cached or args.rescore):
            raise ConfigError(
                "baseline требует полного нового прогона без --only/--cached/--rescore"
            )
        config = load_config()
        budget = None
        if args.discovery_model and args.rescore:
            raise ConfigError("--discovery-model не применяется к --rescore")
        if args.budget_usd is not None:
            if args.cached or args.rescore:
                raise ConfigError("--budget-usd применяется только к новому живому прогону")
            rates = config["live_budget"]
            budget = Budget(
                args.budget_usd,
                Decimal(rates["input_per_mtok"]),
                Decimal(rates["output_per_mtok"]),
                rates["model"],
                {
                    model: (Decimal(r["input_per_mtok"]), Decimal(r["output_per_mtok"]))
                    for model, r in rates.get("models", {}).items()
                },
            )
            known = {rates["model"], *budget.other_models}
            if args.discovery_model and args.discovery_model not in known:
                raise ConfigError(f"нет тарифа {args.discovery_model} в [live_budget.models]")
        cases = load_cases()
        if args.only:
            cases = [c for c in cases if c.id in args.only]
            if not cases:
                raise ConfigError(f"нет документов {args.only} в dataset.jsonl")
        goldens = {case.id: checked_golden(case) for case in cases}
        if args.update_baseline:
            unreviewed = [key for key, g in goldens.items() if not (g.verified_by or "").strip()]
            if unreviewed:
                raise ConfigError(f"эталон не проверен экспертом: {', '.join(unreviewed)}")
        if args.rescore:
            outcomes = stored_outcomes(Path(args.rescore), cases)
        else:
            s6 = args.s6 or args.judge
            outcomes = asyncio.run(
                run_documents(
                    cases,
                    cached=args.cached,
                    budget=budget,
                    s6=s6,
                    discovery_model=args.discovery_model,
                )
            )
        judged: dict[str, dict[str, float | None]] = {}
        if args.judge:
            model = str(config.get("judge", {}).get("model", ""))
            if not model:
                raise ConfigError("нет [judge] model в evals/config.toml")
            judged = asyncio.run(judge_documents(outcomes, model, budget))
        scores: dict[str, DocumentScore] = {}
        for doc_id, outcome in outcomes.items():
            if not isinstance(outcome, str):
                scores[doc_id] = score_document(goldens[doc_id], outcome.prediction)
    except (ConfigError, ValueError, KeyError, ArithmeticError) as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 2

    per_document: dict[str, dict[str, float | None]] = {}
    for doc_id, score in scores.items():
        outcome = outcomes[doc_id]
        assert not isinstance(outcome, str)
        usage = {} if args.cached else {k: outcome.usage.get(k) for k in USAGE}
        s6_metrics = score_opportunities(outcome.opportunities) if outcome.opportunities else {}
        per_document[doc_id] = {**score.metrics, **s6_metrics, **judged.get(doc_id, {}), **usage}
    summary = aggregate(per_document)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    RUNS_DIR.mkdir(exist_ok=True)
    run_file = RUNS_DIR / f"{stamp}.json"
    record = {
        "stamp": stamp,
        "cached": args.cached,
        "rescored_from": args.rescore,
        "budget": budget.report() if budget else None,
        "documents": {
            doc_id: {
                "error": outcome if isinstance(outcome, str) else None,
                "outcome": None if isinstance(outcome, str) else outcome.model_dump(mode="json"),
                "score": scores[doc_id].model_dump(mode="json") if doc_id in scores else None,
                "golden_verified_by": goldens[doc_id].verified_by,
            }
            for doc_id, outcome in outcomes.items()
        },
        "summary": summary,
    }
    run_file.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", "utf-8")

    partial = bool(args.only)
    print(f"\n## eval {stamp}: {len(cases)} док." + (" (частичный)" if partial else ""))
    unverified = [doc_id for doc_id, g in goldens.items() if g.verified_by is None]
    if unverified:
        print(f"⚠ эталон не проверен экспертом: {', '.join(unverified)}")
    print_documents(scores, outcomes)
    print_s6(per_document)

    run_models = next((o.models for o in outcomes.values() if not isinstance(o, str)), None)
    baseline = None if partial else _load_baseline([case.id for case in cases], run_models)
    problems, rows = gate(summary, baseline, config, strict=args.strict)
    print("\n| метрика | baseline | сейчас | Δ | статус |\n|---|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print(f"\nдетали: {run_file.relative_to(ROOT)}")
    if budget:
        print(f"Расход провайдера: ${budget.actual_usd}; лимит: ${budget.limit_usd}")

    failed = [
        doc_id
        for doc_id, outcome in outcomes.items()
        if isinstance(outcome, str)
        or outcome.status != "succeeded"
        or not outcome.complete
        or outcome.failed_processes > 0
    ]
    if failed:
        problems.append(f"неполный или неуспешный анализ: {', '.join(failed)}")
    if args.update_baseline:
        # Ceilings, targets and incomplete analyses are never accepted; a regression against the
        # old baseline is, with --accept-regressions and its reason kept in the new baseline.
        hard = set(gate(summary, None, config, strict=args.strict)[0])
        regressions = [p for p in problems if p not in hard and not p.startswith("неполный")]
        blocking = [p for p in problems if p not in regressions or not args.accept_regressions]
        if blocking:
            print("eval: baseline не обновлён: " + "; ".join(blocking), file=sys.stderr)
            return 1
        accepted = (
            {"reason": args.accept_regressions, "regressions": regressions} if regressions else None
        )
        _write_baseline(stamp, outcomes, per_document, summary, accepted)
        print(f"baseline обновлён: {BASELINE.relative_to(ROOT)}")
        return 0
    if partial:
        return 1 if failed else 0
    if baseline is None:
        print("baseline нет — зафиксируй: uv run python -m evals.run --update-baseline")
    if problems:
        print("\n❌ " + "\n❌ ".join(problems))
        return 1
    return 0


def _load_baseline(
    documents: list[str], models: dict[str, str] | None = None
) -> dict[str, float | None] | None:
    """The baseline of these documents; one of another eval set compares nothing (a new
    document shifts every `.min` and `.mean`), and the next full run replaces it."""
    if not BASELINE.exists():
        return None
    stored = json.loads(BASELINE.read_text("utf-8"))
    if sorted(stored.get("documents", {})) != sorted(documents):
        print(f"baseline снят на другом наборе ({', '.join(sorted(stored.get('documents', {})))}): "
              "без сравнения")  # fmt: skip
        return None
    if models and stored.get("models") and stored["models"] != models:
        print(f"⚠ baseline снят на моделях {stored['models']}, сейчас {models}: "
              "разница метрик может быть делом модели")  # fmt: skip
    metrics: dict[str, float | None] = stored["metrics"]
    return metrics


def _write_baseline(
    stamp: str,
    outcomes: dict[str, Outcome | str],
    per_document: dict[str, dict[str, float | None]],
    summary: dict[str, float | None],
    accepted: dict[str, object] | None = None,
) -> None:
    first = next(o for o in outcomes.values() if not isinstance(o, str))
    payload = {
        "updated": stamp,
        "models": first.models,
        "prompt_versions": first.prompt_versions,
        "metrics": summary,
        "documents": per_document,
    }
    if accepted:
        payload["accepted_regressions"] = accepted
    BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text.replace(",", "."))
    except ArithmeticError:
        raise argparse.ArgumentTypeError(f"не число: {text}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", nargs="+", metavar="ID", help="только эти документы, без гейта")
    parser.add_argument("--cached", action="store_true", help="ответы LLM из кэша")
    parser.add_argument("--rescore", metavar="RUN", help="пересчитать метрики сохранённого прогона")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--strict", action="store_true", help="проверять и цели SC-003")
    parser.add_argument(
        "--accept-regressions",
        metavar="REASON",
        help="с --update-baseline: принять регрессии к прошлому baseline, причина сохраняется",
    )
    parser.add_argument("--s6", action="store_true", help="с этапом S6: метрики инициатив")
    parser.add_argument(
        "--judge", action="store_true", help="S6 и LLM-судья (модель [judge]); вердикты в кэше"
    )
    parser.add_argument(
        "--budget-usd", type=_decimal, help="лимит живого прогона; тарифы live_budget"
    )
    parser.add_argument(
        "--discovery-model", metavar="MODEL", help="своя модель для S3 (LLM_MODEL_DISCOVERY)"
    )
    return _main(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
