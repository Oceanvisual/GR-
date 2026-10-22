"""A restart in the middle of S4 (NFR-013, Q13, T057): data survive, the run is interrupted and
resumes from where it stopped; the process S4 had finished is not asked again.

Two ways a service stops: `docker compose restart` (SIGTERM: the app shuts down, the runner
marks the run interrupted) and a crash (kill -9: the run is left `running` in the database, the
next start marks it interrupted).
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from pipeline_helpers import no_change_for_s6
from pydantic import BaseModel
from test_analysis_e2e import LLM, Env
from test_regenerate import CONTROL, CONTROL_DETAIL, card_of, labelled

from gr.app import create_app
from gr.domain.enums import RunStatus, StageName, StageStatus
from gr.llm.base import LLMResult
from gr.llm.fake import FakeLLM
from gr.settings import Settings
from gr.storage.db import Database
from gr.storage.repositories import Repositories


class HeldLLM(FakeLLM):
    """FakeLLM whose S4 call for the control process never returns: the service stops there."""

    def __init__(self) -> None:
        super().__init__(default=no_change_for_s6)
        self.held = asyncio.Event()

    async def generate[T: BaseModel](self, **request: Any) -> LLMResult[T]:
        if (
            request["stage"] == StageName.DETAIL
            and "Контроль" in request["user"].split("</process_card>")[0]
        ):
            self.held.set()
            await asyncio.Event().wait()  # until the service stops
        return await super().generate(**request)


@asynccontextmanager
async def service(settings: Settings, llm: FakeLLM) -> AsyncIterator[Env]:
    """The application running with its lifespan, as in production."""
    app = create_app(settings, llm=llm)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as client,
    ):
        yield Env(client, app, llm)


async def stop_in_s4(env: Env, llm: HeldLLM) -> tuple[str, str]:
    """Start an analysis of two processes; return when S4 has saved the first one and holds
    the second. Gives the run and the key of the saved process."""
    doc_id = await env.upload()
    discover = json.loads(await env.answer(doc_id, "discover_correct"))
    discover["processes"].append(await labelled(env, doc_id, CONTROL))
    discover["non_process_refs"] = [r for r in discover["non_process_refs"] if "4." not in r]
    simple = json.loads(await env.answer(doc_id, "detail_simple"))
    control = await labelled(env, doc_id, CONTROL_DETAIL)
    llm.add(StageName.DISCOVER, discover)
    llm.add(StageName.DETAIL, lambda call: control if "Контроль" in card_of(call) else simple)
    llm.add(StageName.DETAIL, lambda call: control if "Контроль" in card_of(call) else simple)
    run_id: str = (await env.client.post(f"/api/v1/documents/{doc_id}/analyses")).json()["id"]
    await asyncio.wait_for(llm.held.wait(), timeout=30)
    repos = env.state.repos
    found = await repos.stage_outputs.get(run_id, StageName.DISCOVER)
    assert found is not None
    first = found["processes"][0]["key"]
    store = repos.stage_items.for_stage(run_id, StageName.DETAIL)
    for _ in range(300):  # the first process is saved when its answer is in
        if await store.get(first) is not None:
            break
        await asyncio.sleep(0.01)
    assert await store.get(first) is not None
    return run_id, first


async def left_running(settings: Settings, run_id: str) -> None:
    """What kill -9 leaves behind: the run and its S4 still `running` in the database."""
    db = Database(settings.resolved_database_url)
    await db.init()
    try:
        repos = Repositories(db)
        run = await repos.runs.get(run_id)
        detail = run.stage(StageName.DETAIL).model_copy(update={"status": StageStatus.RUNNING})
        await repos.runs.update(
            run.with_stage(detail).model_copy(update={"status": RunStatus.RUNNING})
        )
    finally:
        await db.dispose()


@pytest.mark.parametrize("way", ["restart", "crash"])
async def test_a_run_stopped_in_s4_resumes_without_repeating_finished_processes(
    way: str, tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    settings = make_settings(data_dir=tmp_path / "data", ocr_enabled=False, **LLM)
    held = HeldLLM()
    async with service(settings, held) as env:
        run_id, first = await stop_in_s4(env, held)
    if way == "crash":
        await left_running(settings, run_id)

    fresh = FakeLLM(default=no_change_for_s6)  # a new process: nothing in memory
    async with service(settings, fresh) as env:
        stopped = (await env.client.get(f"/api/v1/analyses/{run_id}")).json()
        assert stopped["status"] == "interrupted", stopped
        statuses = {s["name"]: s["status"] for s in stopped["stages"]}
        assert statuses["discover"] == "succeeded" and statuses["detail"] == "pending"
        assert (await env.client.get(f"/analyses/{run_id}")).status_code == 200
        doc_id = stopped["doc_id"]
        control = await labelled(env, doc_id, CONTROL_DETAIL)
        fresh.add(StageName.DETAIL, control)  # only the process S4 had not finished

        resumed = await env.client.post(f"/api/v1/analyses/{run_id}/resume")
        assert resumed.status_code == 202, resumed.text
        done = await env.finish(run_id)
        assert done["status"] == "succeeded", done
        assert [c.stage for c in fresh.calls_for(StageName.DISCOVER)] == []  # S3 not repeated
        (call,) = fresh.calls_for(StageName.DETAIL)  # S4 asked for the held process only
        assert "Контроль" in card_of(call) and first not in card_of(call)
        processes = (await env.client.get(f"/api/v1/analyses/{run_id}/processes")).json()
        assert len(processes["processes"]) == 2
        attempts = {s["name"]: s["attempts"] for s in done["stages"]}
        assert attempts["discover"] == 1 and attempts["detail"] == 2
