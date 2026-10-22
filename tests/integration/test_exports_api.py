"""Q9 through the API and screen 4 (api.md § Экспорт, FR-092, T055): files to save, valid by
the published schema, the scenario named, the numbers of the screen."""

import csv
import io
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from test_analysis_e2e import make_env as make_env
from test_exports import decided

from gr.domain.enums import Scenario
from gr.export.csv_export import MONEY, number
from gr.export.json_export import AnalysisExport, export_schema
from gr.services.exports import ExportService
from gr.web.format import money


async def test_q9_exports_through_the_api(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id, key = await decided(env)
        api = f"/api/v1/analyses/{run_id}"
        calls = len(env.llm.calls)

        schema = await env.client.get("/api/v1/schema/analysis-export.json")
        assert schema.status_code == 200 and schema.json() == export_schema()

        exported = await env.client.get(f"{api}/export.json?scenario=optimistic")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/json"
        assert exported.headers["content-disposition"] == (
            f'attachment; filename="analysis-{run_id}-optimistic.json"'
        )
        data = json.loads(exported.content)
        AnalysisExport.model_validate_json(exported.content)  # the model the schema comes from
        published = schema.json()
        assert set(data) == set(published["properties"])
        assert set(published["required"]) <= set(data)
        assert data["scenario"] == "optimistic"  # «сценарий указан»

        table = await env.client.get(f"{api}/export/initiatives.csv?scenario=optimistic")
        assert table.status_code == 200
        assert table.headers["content-type"] == "text/csv; charset=utf-8"
        assert table.headers["content-disposition"] == (
            f'attachment; filename="initiatives-{run_id}-optimistic.csv"'
        )
        assert table.content == await ExportService(env.state).initiatives_csv(
            run_id, Scenario.OPTIMISTIC
        )
        rows = list(csv.DictReader(io.StringIO(table.content.decode("utf-8-sig")), delimiter=";"))
        (line,) = [r for r in rows if r["initiative_id"] == key["i1"]]
        assert line["scenario"] == "Оптимистичный"
        shown = (await env.client.get(f"{api}/initiatives?scenario=optimistic")).json()
        (row,) = [r for r in shown["rows"] if r["initiative"]["id"] == key["i1"]]
        exact = Decimal(row["economics"]["solo"]["cum_net"])
        assert line["cum_net_horizon"] == number(exact, MONEY)  # the numbers of the table
        screen = (await env.client.get(f"/analyses/{run_id}/initiatives?scenario=optimistic")).text
        assert money(exact) in screen  # and of the screen, from the same value

        operations = await env.client.get(f"{api}/export/operations.csv")
        assert operations.status_code == 200
        assert operations.headers["content-disposition"] == (
            f'attachment; filename="operations-{run_id}.csv"'
        )
        assert operations.content.decode("utf-8-sig").startswith("process_id;process_name;")

        missing = await env.client.get("/api/v1/analyses/run_unknown/export.json")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
        wrong = await env.client.get(f"{api}/export/initiatives.csv?scenario=best")
        assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "validation_error"
        assert len(env.llm.calls) == calls  # no LLM call


async def test_the_screen_offers_the_files_of_its_scenario(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id, _ = await decided(env)
        page = (await env.client.get(f"/analyses/{run_id}/initiatives?scenario=conservative")).text
        api = f"/api/v1/analyses/{run_id}"
        assert f'href="{api}/export.json?scenario=conservative" download' in page
        assert f'href="{api}/export/initiatives.csv?scenario=conservative" download' in page
        assert f'href="{api}/export/operations.csv" download' in page
        assert 'href="/api/v1/schema/analysis-export.json"' in page
        assert "сценарий «Консервативный»" in page
