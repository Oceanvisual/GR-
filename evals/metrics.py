"""Extraction metrics against the golden annotation (R-17, SC-002…SC-004). No LLM, no I/O.

The pipeline result comes in as a `Prediction`: processes reduced to the fragment labels they
cite, so the metrics are testable on toy data and a stored run can be rescored after the
golden annotation changes.

Matching:
- a predicted process is described by the fragments its name and operation actions cite;
  it matches a golden process when at least `MIN_SHARE` of those fragments lie in the golden
  roots (with subtrees), or when the names are close (`NAME_MATCH`) and they share a fragment.
  Pairs are taken greedily, best first, one to one: a second process on the same procedure is
  a false positive;
- a predicted operation matches a golden operation of the matched process when its action
  cites a fragment of that operation (with subtree); several candidates are told apart by name.
"""

import re
import statistics
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel
from rapidfuzz import fuzz

from evals.golden import Golden, GoldenProcess
from gr.domain.enums import DeadlineUnit
from gr.grounding.deadlines import check_deadline

MIN_SHARE = 0.5
NAME_MATCH = 85.0

# Quality metrics: higher is better (True) or lower is better (False).
QUALITY: dict[str, bool] = {
    "process_recall": True,
    "process_precision": True,
    "operation_recall": True,
    "deadline_recall": True,
    "verified_share": True,
    "coverage": True,
    "deadlines_not_in_text": False,
}
USAGE = ("calls", "in_tokens", "out_tokens", "latency_s")


# --- prediction ----------------------------------------------------------------------------


class FragmentNode(BaseModel):
    label: str
    parent: int | None  # index of the parent in Prediction.fragments


class PredOperation(BaseModel):
    name: str
    labels: list[str]  # fragments cited by the action


class PredDeadline(BaseModel):
    value: int | None
    unit: str
    spans: list[str]  # document text at each citation of the deadline


class PredProcess(BaseModel):
    name: str
    labels: list[str]  # fragments cited by the name and the operation actions
    operations: list[PredOperation] = []
    deadlines: list[PredDeadline] = []  # of the process and of its operations


class Prediction(BaseModel):
    fragments: list[FragmentNode]
    processes: list[PredProcess]
    citations_passed: int = 0  # fact citations that passed verification
    citations_total: int = 0  # all fact citations
    unverified: int = 0  # claims kept out of the facts
    fragments_used: int = 0
    fragments_total: int = 0


# --- scores --------------------------------------------------------------------------------


class ProcessMatch(BaseModel):
    golden: str
    predicted: str
    share: float
    name_score: float
    operations_found: int
    operations_total: int
    missed_operations: list[str]


class DocumentScore(BaseModel):
    metrics: dict[str, float | None]
    matches: list[ProcessMatch]
    missed: list[str]  # required golden processes not found
    extra: list[str]  # predicted processes that match no golden process
    bad_deadlines: list[str]  # deadline facts whose value is not in their cited text


class UnknownLabelError(ValueError):
    """The golden annotation names a fragment the parsed document does not have."""


def subtree_labels(fragments: Sequence[FragmentNode], roots: Iterable[str]) -> set[str]:
    """Labels of the fragments that are, or descend from, a fragment labelled as a root."""
    wanted = set(roots)
    known = {f.label for f in fragments}
    if missing := wanted - known:
        raise UnknownLabelError(f"no fragments labelled {sorted(missing)}")
    labels: set[str] = set()
    for node in fragments:
        current: FragmentNode | None = node
        while current is not None:
            if current.label in wanted:
                labels.add(node.label)
                break
            current = fragments[current.parent] if current.parent is not None else None
    return labels


_PUNCT_RE = re.compile(r"[^\w\s]")


def _norm(name: str) -> str:
    return _PUNCT_RE.sub(" ", name.lower().replace("ё", "е"))


def name_score(predicted: str, golden: GoldenProcess | str) -> float:
    names = [golden] if isinstance(golden, str) else [golden.name, *golden.aliases]
    return max(fuzz.token_set_ratio(_norm(predicted), _norm(n)) for n in names)


def _match_operations(
    predicted: PredProcess, golden: GoldenProcess, fragments: Sequence[FragmentNode]
) -> tuple[int, list[str]]:
    spans = [subtree_labels(fragments, op.labels) for op in golden.operations]
    used: set[int] = set()
    for op in predicted.operations:
        cited = set(op.labels)
        candidates = [i for i, span in enumerate(spans) if i not in used and cited & span]
        if candidates:
            used.add(max(candidates, key=lambda i: name_score(op.name, golden.operations[i].name)))
    missed = [g.name for i, g in enumerate(golden.operations) if i not in used]
    return len(used), missed


_NEXT_DAY = {(None, DeadlineUnit.NEXT_WORKING_DAY.value), (1, DeadlineUnit.WORKING_DAYS.value)}


def _deadline_found(deadline: PredDeadline, golden_value: int | None, golden_unit: str) -> bool:
    # «Не позднее 1 рабочего дня, следующего за днём получения» reads either way.
    if {(deadline.value, deadline.unit), (golden_value, golden_unit)} <= _NEXT_DAY:
        return True
    if deadline.value != golden_value:
        return False
    days = {DeadlineUnit.DAYS.value, DeadlineUnit.CALENDAR_DAYS.value}
    return deadline.unit == golden_unit or {deadline.unit, golden_unit} <= days


def _in_text(deadline: PredDeadline) -> bool:
    unit = DeadlineUnit(deadline.unit)
    return any(check_deadline(deadline.value, unit, span).ok for span in deadline.spans)


def _ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def score_document(golden: Golden, prediction: Prediction) -> DocumentScore:
    fragments = prediction.fragments
    spans = {g.key: subtree_labels(fragments, g.roots) for g in golden.processes}

    candidates: list[tuple[tuple[float, int, float], int, GoldenProcess]] = []
    for n, pred in enumerate(prediction.processes):
        cited = set(pred.labels)
        for gold in golden.processes:
            overlap = len(cited & spans[gold.key])
            share = overlap / len(cited) if cited else 0.0
            similar = name_score(pred.name, gold)
            if share >= MIN_SHARE or (similar >= NAME_MATCH and overlap > 0):
                candidates.append(((share, overlap, similar), n, gold))
    candidates.sort(key=lambda c: c[0], reverse=True)

    taken_pred: set[int] = set()
    pred_of: dict[str, int] = {}  # golden key → index of the matched predicted process
    matches: list[ProcessMatch] = []
    for (share, _overlap, similar), n, gold in candidates:
        if n in taken_pred or gold.key in pred_of:
            continue
        taken_pred.add(n)
        pred_of[gold.key] = n
        found, missed_ops = _match_operations(prediction.processes[n], gold, fragments)
        matches.append(
            ProcessMatch(
                golden=gold.key,
                predicted=prediction.processes[n].name,
                share=round(share, 3),
                name_score=round(similar, 1),
                operations_found=found,
                operations_total=len(gold.operations),
                missed_operations=missed_ops,
            )
        )

    required = golden.required()
    by_key = {m.golden: m for m in matches}
    matched_required = [g for g in required if g.key in by_key]
    found_ops = sum(by_key[g.key].operations_found for g in matched_required)
    total_ops = sum(len(g.operations) for g in matched_required)
    operation_recall = _ratio(found_ops, total_ops)
    if operation_recall is None and any(g.operations for g in required):
        operation_recall = 0.0  # nothing found: not «undefined», a miss

    golden_deadlines = 0
    found_deadlines = 0
    for gold in matched_required:
        pred = prediction.processes[pred_of[gold.key]]
        for expected in gold.deadlines:
            golden_deadlines += 1
            if any(_deadline_found(d, expected.value, expected.unit) for d in pred.deadlines):
                found_deadlines += 1

    bad = [
        f"{p.name}: {d.value} {d.unit}"
        for p in prediction.processes
        for d in p.deadlines
        if not _in_text(d)
    ]
    predicted = len(prediction.processes)
    metrics: dict[str, float | None] = {
        "process_recall": _ratio(len(matched_required), len(required)),
        "process_precision": len(taken_pred) / predicted if predicted else 0.0,
        "operation_recall": operation_recall,
        "deadline_recall": _ratio(found_deadlines, golden_deadlines),
        "verified_share": _ratio(
            prediction.citations_passed, prediction.citations_total + prediction.unverified
        ),
        "coverage": _ratio(prediction.fragments_used, prediction.fragments_total),
        "deadlines_not_in_text": float(len(bad)),
        "processes_predicted": float(predicted),
        "unverified": float(prediction.unverified),
    }
    return DocumentScore(
        metrics=metrics,
        matches=matches,
        missed=[g.name for g in required if g.key not in by_key],
        extra=[p.name for n, p in enumerate(prediction.processes) if n not in taken_pred],
        bad_deadlines=bad,
    )


# --- aggregation and gate ------------------------------------------------------------------


def aggregate(per_document: Mapping[str, Mapping[str, float | None]]) -> dict[str, float | None]:
    """`<metric>.mean` and `<metric>.min` over documents for quality metrics (R-21: a good
    result on one document must not hide a failure on another); sums for usage and for
    deadlines not in the text, which must be zero everywhere."""
    out: dict[str, float | None] = {}
    for name in QUALITY:
        values = [v for doc in per_document.values() if (v := doc.get(name)) is not None]
        if name == "deadlines_not_in_text":
            out[f"{name}.sum"] = sum(values) if values else None
            continue
        out[f"{name}.mean"] = round(statistics.fmean(values), 4) if values else None
        out[f"{name}.min"] = round(min(values), 4) if values else None
    for name in USAGE:
        values = [v for doc in per_document.values() if (v := doc.get(name)) is not None]
        out[f"{name}.sum"] = round(sum(values), 1) if values else None
    return out


def _higher_is_better(key: str) -> bool | None:
    name = key.rsplit(".", 1)[0]
    if name in QUALITY:
        return QUALITY[name]
    if name in USAGE:
        return False
    return None


def gate(
    current: Mapping[str, float | None],
    baseline: Mapping[str, float | None] | None,
    config: Mapping[str, object],
    *,
    strict: bool = False,
) -> tuple[list[str], list[tuple[str, str, str, str, str]]]:
    """Violations and table rows (metric, baseline, now, delta, status).

    Quality: worse than baseline by more than `gate.tolerance` (absolute; per metric in
    `gate.tolerances`). Usage: only with a per-metric relative tolerance (0.5 = +50 %).
    Ceilings in `gate.max` always apply; `targets` (SC-003, SC-002) only with `strict`.
    """
    gate_cfg = config.get("gate", {})
    assert isinstance(gate_cfg, dict)
    default_tol = float(gate_cfg.get("tolerance", 0.05))
    tolerances: dict[str, float] = gate_cfg.get("tolerances", {})
    ceilings: dict[str, float] = gate_cfg.get("max", {})
    targets = config.get("targets", {})
    assert isinstance(targets, dict)
    problems: list[str] = []
    rows: list[tuple[str, str, str, str, str]] = []
    for key, value in current.items():
        better_up = _higher_is_better(key)
        if value is None or better_up is None:
            continue
        name = key.rsplit(".", 1)[0]
        status = ""
        base = (baseline or {}).get(key)
        delta = ""
        if base is not None:
            diff = value - base
            delta = f"{diff:+.3f}"
            worse_by = -diff if better_up else diff
            if name in USAGE:
                tol = tolerances.get(key, tolerances.get(name))
                regressed = tol is not None and worse_by > float(tol) * abs(base)
            else:
                tol = float(tolerances.get(key, tolerances.get(name, default_tol)))
                regressed = worse_by > tol + 1e-9
            if regressed:
                status = "❌ регрессия"
                problems.append(f"{key}: {base} → {value} (допуск {tol})")
            elif worse_by < -1e-9:
                status = "✅ лучше"
            elif worse_by > 1e-9:
                status = "· хуже"
            else:
                status = "✅"
        limit = ceilings.get(key, ceilings.get(name))
        if limit is not None and value > float(limit):
            status = f"❌ > {limit}"
            problems.append(f"{key}: {value} > max {limit}")
        target = targets.get(name)
        if strict and target is not None and key.endswith((".mean", ".min")):
            missed = value < float(target) if better_up else value > float(target)
            if missed:
                status = f"❌ цель {target}"
                problems.append(f"{key}: {value} не достигает цели {target}")
        rows.append((key, "" if base is None else f"{base:g}", f"{value:g}", delta, status))
    return problems, rows
