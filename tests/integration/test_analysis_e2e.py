"""Analysis end to end on FakeLLM: upload → analysis → verified process map (T032, Q11, Q12)."""

import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pipeline_helpers import no_change_for_s6

from gr.api.deps import AppState
from gr.app import create_app
from gr.domain.enums import RunStatus, StageName
from gr.domain.ids import new_id
from gr.domain.run import AnalysisRun, UsageTotals
from gr.extraction.render import FragmentAliases
from gr.llm.base import LLMTimeoutError
from gr.llm.fake import FakeLLM
from gr.services.analyses import price
from gr.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_LABEL_RE = re.compile(r"@([^@]+)@")
LLM = {
    "llm_data_policy": "local_only",
    "llm_base_url": "http://localhost:11434/v1",
    "llm_model_extraction": "fake-extract",
    "llm_model_analysis": "fake-analyze",
}


@dataclass
class Env:
    client: httpx.AsyncClient
    app: FastAPI
    llm: FakeLLM

    @property
    def state(self) -> AppState:
        state: AppState = self.app.state.gr
        return state

    async def upload(self, name: str = "regulation.txt", folder: str = "synthetic") -> str:
        data = (FIXTURES / folder / name).read_bytes()
        response = await self.client.post("/api/v1/documents", files={"file": (name, data)})
        assert response.status_code == 201, response.text
        doc_id: str = response.json()["id"]
        return doc_id

    async def answer(self, doc_id: str, name: str) -> str:
        """A fixture answer with `@label@` replaced by the aliases of the uploaded document."""
        fragments = await self.state.repos.fragments.list_for_document(doc_id)
        aliases = FragmentAliases(fragments)
        by_label = {f.label: f for f in fragments}
        text = (FIXTURES / "llm" / f"{name}.json").read_text("utf-8")
        return _LABEL_RE.sub(lambda m: aliases.alias(by_label[m.group(1)].id), text)

    async def finish(self, run_id: str) -> dict[str, Any]:
        await self.state.runner.wait(run_id)
        run: dict[str, Any] = (await self.client.get(f"/api/v1/analyses/{run_id}")).json()
        return run


@pytest.fixture
def make_env(tmp_path: Path, make_settings: Callable[..., Settings]) -> Callable[..., Any]:
    def factory(**overrides: Any) -> Any:
        settings = make_settings(data_dir=tmp_path / "data", ocr_enabled=False, **(LLM | overrides))
        llm = FakeLLM(default=no_change_for_s6)
        app = create_app(settings, llm=llm)

        async def env() -> AsyncIterator[Env]:
            transport = httpx.ASGITransport(app=app)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as c,
            ):
                yield Env(c, app, llm)

        return env()

    return factory


async def test_upload_analyse_and_read_the_process_map(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        doc_id = await env.upload()
        env.llm.add(StageName.DISCOVER, await env.answer(doc_id, "discover_correct"))
        env.llm.add(StageName.DETAIL, await env.answer(doc_id, "detail_simple"))

        started = await env.client.post(f"/api/v1/documents/{doc_id}/analyses")
        assert started.status_code == 202, started.text
        run = started.json()
        assert run["status"] == "queued" and run["doc_id"] == doc_id
        config = run["config_snapshot"]
        assert config["prompt_versions"]["discover"].startswith("discover@")
        assert config["models"]["extraction"] == "fake-extract"
        assert config["grounding_thresholds"]["quote_fuzzy_ok"] == 92

        done = await env.finish(run["id"])
        assert done["status"] == "succeeded", done
        statuses = {s["name"]: s["status"] for s in done["stages"]}
        assert statuses == {
            "discover": "succeeded",
            "detail": "succeeded",
            "validate": "succeeded",
            "opportunities": "succeeded",
            "postprocess": "succeeded",
        }
        assert done["completeness"]["complete"] is True
        assert [c.stage for c in env.llm.calls] == [
            StageName.DISCOVER,
            StageName.DETAIL,
            StageName.OPPORTUNITIES,
        ]

        model = (await env.client.get(f"/api/v1/analyses/{run['id']}/processes")).json()
        (process,) = model["processes"]
        assert process["id"] == "proc_1" and len(process["operations"]) == 2
        for operation in process["operations"]:
            (citation,) = operation["action"]["citations"]
            assert citation["grounding"]["status"] == "verified"
        assert process["deadlines"][0]["value"] == 10
        assert model["unverified"] == [] and model["coverage"]["fragments_total"] > 0

        detail = await env.client.get(f"/api/v1/analyses/{run['id']}/processes/proc_1")
        assert detail.status_code == 200 and detail.json()["name"]["value"]
        assert detail.json()["mermaid"].startswith("flowchart TD\n    op_1[")
        missing = await env.client.get(f"/api/v1/analyses/{run['id']}/processes/proc_9")
        assert missing.status_code == 404

        versions = (await env.client.get(f"/api/v1/documents/{doc_id}/analyses")).json()
        assert [v["id"] for v in versions] == [run["id"]] and versions[0]["status"] == "succeeded"
        cancel = await env.client.post(f"/api/v1/analyses/{run['id']}/cancel")
        assert cancel.status_code == 400


async def test_failed_detail_resumes_without_repeating_discovery(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        doc_id = await env.upload()
        env.llm.add(StageName.DISCOVER, await env.answer(doc_id, "discover_correct"))
        env.llm.add(StageName.DETAIL, LLMTimeoutError())
        run_id = (await env.client.post(f"/api/v1/documents/{doc_id}/analyses")).json()["id"]
        failed = await env.finish(run_id)
        assert failed["status"] == "failed" and failed["error"]["code"] == "llm_timeout"

        env.llm.add(StageName.DETAIL, await env.answer(doc_id, "detail_simple"))
        resumed = await env.client.post(f"/api/v1/analyses/{run_id}/resume")
        assert resumed.status_code == 202
        done = await env.finish(run_id)
        assert done["status"] == "succeeded"
        assert [c.stage for c in env.llm.calls].count(StageName.DISCOVER) == 1

        again = await env.client.post(f"/api/v1/analyses/{run_id}/resume")
        assert again.status_code == 400


async def test_second_start_while_running_is_409(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        doc_id = await env.upload()
        active = AnalysisRun(
            id=new_id("run"), doc_id=doc_id, created_by="expert", created_at=datetime.now(UTC)
        ).transition(RunStatus.RUNNING)
        await env.state.repos.runs.add(active)
        response = await env.client.post(f"/api/v1/documents/{doc_id}/analyses")
        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "run_in_progress",
            "message": "Анализ этого документа уже выполняется.",
            "details": {"run_id": active.id},
        }


async def test_q11_no_llm_configured(make_env: Callable[..., Any]) -> None:
    async for env in make_env(llm_data_policy="disabled"):
        doc_id = await env.upload()
        response = await env.client.post(f"/api/v1/documents/{doc_id}/analyses")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "llm_not_configured"
        assert env.llm.calls == []
        assert (await env.client.get(f"/api/v1/documents/{doc_id}/analyses")).json() == []


async def test_q12_external_host_under_local_only(make_env: Callable[..., Any]) -> None:
    async for env in make_env(llm_base_url="https://api.example.com/v1"):
        doc_id = await env.upload()
        response = await env.client.post(f"/api/v1/documents/{doc_id}/analyses")
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "external_llm_not_allowed" and "api.example.com" in error["message"]
        assert env.llm.calls == []


async def test_scan_is_not_analyzable(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        doc_id = await env.upload("scan.pdf")
        response = await env.client.post(f"/api/v1/documents/{doc_id}/analyses")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "document_not_analyzable"


async def test_unknown_document_and_run(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        no_doc = await env.client.post("/api/v1/documents/doc_00000000000000000000000000/analyses")
        assert no_doc.status_code == 404
        assert (await env.client.get("/api/v1/analyses/run_X")).status_code == 404
        assert (await env.client.get("/api/v1/analyses/run_X/processes")).status_code == 404


def test_price_from_settings(make_settings: Callable[..., Settings]) -> None:
    usage = UsageTotals(calls=3, in_tokens=46_000, out_tokens=4_000)
    assert price(usage, make_settings()).cost is None
    priced = price(
        usage,
        make_settings(
            llm_price_input_per_mtok=Decimal("0.1"), llm_price_output_per_mtok=Decimal("0.5")
        ),
    )
    assert priced.cost == Decimal("0.0066")
