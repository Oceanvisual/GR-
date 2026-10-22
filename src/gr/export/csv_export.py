"""CSV of the initiative table and of the operations (FR-092, api.md § Экспорт, T054).

For people and spreadsheets: UTF-8 with a BOM, «;» between the columns, a decimal comma and the
values named as on the screens. The initiatives are the rows of the table of the same scenario
(`/initiatives`), rejected ones included, in its order and with its numbers; rounding happens
only here: money to kopecks, hours to tenths, FTE to hundredths, ROI (a ratio) to
ten-thousandths. Text comes from the act and from the model, so a cell that a spreadsheet would
take for a formula is written as text.
"""

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from gr.domain.process import ProcessModel
from gr.services.results import InitiativeTable
from gr.web.labels import LABELS

INITIATIVE_COLUMNS = (
    "rank", "group", "gate", "score", "initiative_id", "title", "process", "operations",
    "problem_patterns", "solution_category", "regulatory_mode", "automation_level",
    "hours_per_year_y1", "fte_y1", "money_equiv_y1", "capex", "opex_y1", "net_y1",
    "cum_net_horizon", "roi_horizon", "payback_months", "in_portfolio", "review_status",
    "scenario", "assumptions_version",
)  # fmt: skip
OPERATION_COLUMNS = (
    "process_id", "process_name", "operation_id", "seq", "name", "performer", "action",
    "operation_type", "deadline_raw", "legally_significant", "fragment_labels", "quotes",
    "grounding", "confidence", "review_status",
)  # fmt: skip
MONEY, HOURS, FTE, RATIO, SCORE = 2, 1, 2, 4, 1  # digits after the comma
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def number(value: Decimal | None, digits: int) -> str:
    if value is None:
        return ""
    rounded = value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN)
    if rounded == 0:
        rounded = abs(rounded)  # no «-0,00»
    return format(rounded, "f").replace(".", ",")


def text(value: str | None) -> str:
    """A text cell: one a spreadsheet would run as a formula is kept as text."""
    value = value or ""
    return f"'{value}" if value.startswith(_FORMULA) else value


def _yes(flag: bool) -> str:
    return "да" if flag else "нет"


def _file(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def initiatives_csv(table: InitiativeTable) -> bytes:
    rows = []
    for row in table.rows:
        item, priority, economics = row.initiative, row.priority, row.economics
        solo = economics.solo if economics else None
        rows.append(
            [
                priority.rank if priority and priority.rank else "",
                LABELS["group"][priority.group] if priority else "",
                LABELS["gate"][item.gate.status] if item.gate else "",
                number(priority.score if priority else None, SCORE),
                item.id,
                text(item.title),
                text(row.process_name),
                ", ".join(item.operation_ids),
                ", ".join(LABELS["problem"][p] for p in row.finding_patterns),
                LABELS["solution"][item.solution_category],
                LABELS["regulatory"][item.regulatory_mode],
                LABELS["automation"][item.automation_level],
                number(solo.hours[0] if solo else None, HOURS),
                number(solo.fte[0] if solo else None, FTE),
                number(solo.money[0] if solo else None, MONEY),
                number(solo.capex if solo else None, MONEY),
                number(solo.opex[0] if solo else None, MONEY),
                number(solo.net[0] if solo else None, MONEY),
                number(solo.cum_net if solo else None, MONEY),
                number(solo.roi if solo else None, RATIO),
                solo.payback_months if solo and solo.payback_months else "",
                _yes(bool(economics and economics.in_portfolio)),
                LABELS["review"][item.review.status],
                LABELS["scenario"][table.scenario],
                table.assumptions_version or "",
            ]
        )
    return _file(INITIATIVE_COLUMNS, rows)


def operations_csv(model: ProcessModel, fragment_labels: Mapping[str, str]) -> bytes:
    """Every operation of the process map as the expert left it; its basis is the citations
    of its action."""
    rows = []
    for process in model.processes:
        for op in process.operations:
            cited = op.action.citations
            rows.append(
                [
                    process.id,
                    text(process.name.value),
                    op.id,
                    op.seq + 1,  # numbered from 1, as on the map
                    text(op.name),
                    text(op.performer.value if op.performer else None),
                    text(op.action.value),
                    LABELS["operation_type"][op.operation_type],
                    text(op.deadline.raw.value if op.deadline else None),
                    _yes(op.legally_significant),
                    " | ".join(fragment_labels.get(c.fragment_id, c.fragment_id) for c in cited),
                    text(" | ".join(c.quote for c in cited)),
                    " | ".join(LABELS["grounding"][c.grounding.status] for c in cited),
                    LABELS["confidence"][op.confidence],
                    LABELS["review"][op.review.status],
                ]
            )
    return _file(OPERATION_COLUMNS, rows)
