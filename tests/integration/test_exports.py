"""Exports of a real run (FR-092, FR-093, Q9, T054): the CSV of initiatives is the table of the
same scenario, the JSON is valid by its model and every element carries its layer."""

import csv
import io
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from test_analysis_e2e import Env
from test_analysis_e2e import make_env as make_env
from test_reviews_api import analyse, ids, review, table

from gr.domain.enums import Scenario
from gr.export.csv_export import FTE, HOURS, INITIATIVE_COLUMNS, MONEY, RATIO, SCORE, number
from gr.export.json_export import AnalysisExport
from gr.services.exports import ExportService
from gr.services.results import MODEL_DISCLAIMER

Json = dict[str, Any]
# CSV column → (where the table keeps the value, digits).
NUMBERS = {
    "hours_per_year_y1": (("hours", 0), HOURS),
    "fte_y1": (("fte", 0), FTE),
    "money_equiv_y1": (("money", 0), MONEY),
    "capex": (("capex", None), MONEY),
    "opex_y1": (("opex", 0), MONEY),
    "net_y1": (("net", 0), MONEY),
    "cum_net_horizon": (("cum_net", None), MONEY),
    "roi_horizon": (("roi", None), RATIO),
}


def rows(data: bytes) -> list[dict[str, str]]:
    assert data.startswith("﻿".encode())
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))


async def decided(env: Env) -> tuple[str, dict[str, str]]:
    """A run with a rejected initiative and a corrected deadline."""
    run_id = await analyse(env)
    key = ids(await table(env, run_id))
    rejected = await review(env, run_id, entity_type="initiative", entity_id=key["i2"],
                            action="reject", comment="Не нужна")  # fmt: skip
    assert rejected.status_code == 201
    deadline = {"value": 2, "unit": "working_days", "raw": "два рабочих дня"}
    edited = await review(env, run_id, entity_type="operation", entity_id="proc_1.op_2",
                          action="edit", patch={"deadline": deadline})  # fmt: skip
    assert edited.status_code == 201
    return run_id, key


async def test_the_csv_of_initiatives_is_the_table_of_its_scenario(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id, key = await decided(env)
        calls = len(env.llm.calls)
        for scenario in Scenario:
            shown = await table(env, run_id, f"?scenario={scenario}&include_rejected=true")
            found = rows(await ExportService(env.state).initiatives_csv(run_id, scenario))
            assert list(found[0]) == list(INITIATIVE_COLUMNS)
            assert [r["initiative_id"] for r in found] == [
                r["initiative"]["id"] for r in shown["rows"]
            ]  # same rows, same order
            for line, row in zip(found, shown["rows"], strict=True):
                solo = row["economics"]["solo"] if row["economics"] else None
                for column, ((field, year), digits) in NUMBERS.items():
                    value = None if solo is None else solo[field]
                    value = value[year] if year is not None and value is not None else value
                    expected = number(Decimal(value) if value is not None else None, digits)
                    assert line[column] == expected, (scenario, column)
                priority = row["priority"]
                score = number(Decimal(priority["score"]), SCORE) if priority else ""
                assert line["score"] == score
                rank = priority["rank"] if priority else None
                assert line["rank"] == (str(rank) if rank else "")  # none when blocked or out
                assert line["assumptions_version"] == str(shown["assumptions_version"])
        optimistic = rows(
            await ExportService(env.state).initiatives_csv(run_id, Scenario.OPTIMISTIC)
        )
        by_id = {r["initiative_id"]: r for r in optimistic}
        assert by_id[key["i2"]]["review_status"] == "Отклонено"
        assert by_id[key["i2"]]["capex"] == "" and by_id[key["i2"]]["in_portfolio"] == "нет"
        assert by_id[key["i1"]]["in_portfolio"] == "да"
        assert by_id[key["i1"]]["scenario"] == "Оптимистичный"
        assert by_id[key["i1"]]["solution_category"] == "Классическая автоматизация"
        assert "," in by_id[key["i1"]]["capex"]  # a decimal comma, for a Russian spreadsheet
        assert len(env.llm.calls) == calls  # no LLM call


async def test_the_csv_of_operations_has_the_corrections(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id, _ = await decided(env)
        found = rows(await ExportService(env.state).operations_csv(run_id))
        assert [(r["operation_id"], r["seq"]) for r in found] == [
            ("proc_1.op_1", "1"),
            ("proc_1.op_2", "2"),
        ]
        second = found[1]
        assert second["deadline_raw"] == "два рабочих дня"
        assert second["review_status"] == "Исправлено"
        assert second["fragment_labels"] == "п. 3.2.2"
        assert second["quotes"] == "проверяет комплектность документов"
        assert second["grounding"] == "цитата найдена" and second["confidence"] == "высокая"


async def test_the_json_export_is_whole_and_valid(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id, key = await decided(env)
        export = await ExportService(env.state).analysis(run_id, Scenario.BASE, "expert")
        written = export.model_dump_json()
        data: Json = json.loads(written)
        # Valid by its own model, and read back it is written the same, byte for byte.
        assert AnalysisExport.model_validate_json(written).model_dump_json() == written
        assert data["schema_version"] == "1.0" and data["exported_by"] == "expert"
        assert data["run"]["id"] == run_id and data["run"]["config_snapshot"]["models"]
        assert data["document"]["id"] == data["run"]["doc_id"] and data["structure"]
        assert set(data["economics"]) == {"conservative", "base", "optimistic"}
        assert data["economics"]["base"]["layer"] == "assumption"
        assert {p["layer"] for p in data["priority"]} == {"hypothesis"}
        assert data["excluded"] == {key["i2"]: "Отклонена экспертом."}
        assert data["initiative_versions"] == {"proc_1": 1}
        assert data["disclaimers"][0] == MODEL_DISCLAIMER
        assert [e["action"] for e in data["reviews"]] == ["edit", "reject", "edit"]  # in order

        elements = [
            *data["processes"],
            *(o for p in data["processes"] for o in p["operations"]),
            *(d for p in data["processes"] for d in p["decision_points"]),
            *data["findings"],
            *data["initiatives"],
        ]
        assert len(elements) >= 6
        for element in elements:  # FR-093
            assert element["layer"] in {"interpretation", "hypothesis"}, element["id"]
            assert element["review"]["status"], element["id"]
        assert {a["layer"] for a in data["assumptions"]["items"]} == {"assumption"}
        (op,) = [o for o in data["processes"][0]["operations"] if o["id"] == "proc_1.op_2"]
        assert op["deadline"]["value"] == 2 and op["review"]["status"] == "edited"
