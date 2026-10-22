"""Analysis screens: start, status with polling, process map (ui.md 2a, 3; T034)."""

import json
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from gr.api.deps import AppState
from gr.app import create_app
from gr.domain.enums import RunStatus, StageName
from gr.domain.ids import new_id
from gr.domain.run import AnalysisRun, Completeness, FailedItem
from gr.extraction.render import FragmentAliases
from gr.llm.fake import FakeLLM
from gr.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_LABEL_RE = re.compile(r"@([^@]+)@")
LLM = {
    "llm_data_policy": "local_only",
    "llm_base_url": "http://localhost:11434/v1",
    "llm_model_extraction": "fake-extract",
    "llm_model_analysis": "fake-analyze",
}
HALLUCINATION = "специалист выплачивает себе премию за каждый выданный пропуск"
HOSTILE_NAME = "<script>alert('op')</script> Проверка"


class Env:
    def __init__(self, client: httpx.AsyncClient, state: AppState, llm: FakeLLM) -> None:
        self.client, self.state, self.llm = client, state, llm

    async def upload(self, name: str = "regulation.txt") -> str:
        data = (FIXTURES / "synthetic" / name).read_bytes()
        response = await self.client.post("/api/v1/documents", files={"file": (name, data)})
        doc_id: str = response.json()["id"]
        return doc_id

    async def answer(self, doc_id: str, name: str) -> dict[str, Any]:
        fragments = await self.state.repos.fragments.list_for_document(doc_id)
        aliases = FragmentAliases(fragments)
        by_label = {f.label: f for f in fragments}
        text = (FIXTURES / "llm" / f"{name}.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(
            _LABEL_RE.sub(lambda m: aliases.alias(by_label[m.group(1)].id), text)
        )
        return result

    async def analysed(self) -> tuple[str, str]:
        """Upload, run the analysis through the web form, wait; (doc_id, run_id)."""
        doc_id = await self.upload()
        self.llm.add(StageName.DISCOVER, await self.answer(doc_id, "discover_correct"))
        detail = await self.answer(doc_id, "detail_window_1")
        fragments = await self.state.repos.fragments.list_for_document(doc_id)
        alias = FragmentAliases(fragments).alias(
            next(f.id for f in fragments if f.label == "п. 3.2.1")
        )
        detail["operations"] += [
            {"key": "o8", "name": "Премия", "depends_on": ["o2"],
             "action": {"text": HALLUCINATION, "ref": alias, "quote": HALLUCINATION}},
            {"key": "o9", "name": HOSTILE_NAME, "depends_on": ["o2"],
             "action": {"text": "вносит в систему", "ref": alias,
                        "quote": "регистрирует его в системе электронного документооборота"}},
        ]  # fmt: skip
        self.llm.add(StageName.DETAIL, detail)
        started = await self.client.post(f"/documents/{doc_id}/analyses")
        assert started.status_code == 303, started.text
        run_id = started.headers["location"].rsplit("/", 1)[1]
        await self.state.runner.wait(run_id)
        return doc_id, run_id


@pytest.fixture
def make_env(tmp_path: Path, make_settings: Callable[..., Settings]) -> Callable[..., Any]:
    def factory(**overrides: Any) -> AsyncIterator[Env]:
        settings = make_settings(data_dir=tmp_path / "data", ocr_enabled=False, **(LLM | overrides))
        llm = FakeLLM()
        app = create_app(settings, llm=llm)

        async def env() -> AsyncIterator[Env]:
            transport = httpx.ASGITransport(app=app)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=transport, base_url="http://test") as client,
            ):
                yield Env(client, app.state.gr, llm)

        return env()

    return factory


async def test_document_page_offers_the_analysis(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        doc_id = await env.upload()
        html = (await env.client.get(f"/documents/{doc_id}")).text
        assert f'action="/documents/{doc_id}/analyses"' in html and "Запустить анализ" in html
        scan = await env.upload("scan.pdf")
        assert "Запустить анализ" not in (await env.client.get(f"/documents/{scan}")).text


async def test_status_page_after_the_run(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        doc_id, run_id = await env.analysed()
        html = (await env.client.get(f"/analyses/{run_id}")).text
        assert "завершён" in html and "S3 · карта процессов" in html and "пропущен" in html
        assert "Вызовов LLM:" in html
        assert f'href="/analyses/{run_id}/processes"' in html
        assert "hx-trigger" not in html  # finished: no polling
        assert "Отменить" not in html and "Продолжить" not in html
        listed = (await env.client.get(f"/documents/{doc_id}")).text
        assert f'href="/analyses/{run_id}"' in listed and "карта процессов" in listed
        assert f'href="/analyses/{run_id}"' in (await env.client.get("/")).text


async def test_active_run_polls_and_can_be_cancelled(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        doc_id = await env.upload()
        run = AnalysisRun(
            id=new_id("run"), doc_id=doc_id, created_by="expert", created_at=datetime.now(UTC)
        ).transition(RunStatus.RUNNING)
        await env.state.repos.runs.add(run)
        status = (await env.client.get(f"/analyses/{run.id}/status")).text
        assert 'hx-trigger="every 2s"' in status and f'hx-get="/analyses/{run.id}/status"' in status
        assert "<html" not in status and "Отменить" in status
        cancelled = await env.client.post(f"/analyses/{run.id}/cancel")
        assert cancelled.status_code == 303
        assert "отменён" in (await env.client.get(f"/analyses/{run.id}")).text


async def test_process_map_has_every_section(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        doc_id, run_id = await env.analysed()
        response = await env.client.get(f"/analyses/{run_id}/processes")
        assert response.status_code == 200
        html = response.text
        for section in ('id="process"', 'id="diagram"', 'id="operations"', 'id="decisions"',
                        'id="validation"', 'id="unverified"', 'id="coverage"'):  # fmt: skip
            assert section in html, section
        assert 'class="process-list' in html and "Выдача пропуска" in html
        assert '<pre class="mermaid">flowchart TD' in html
        assert "vendor/mermaid.min.js" in html and "processes.js" in html
        assert ">Факт</span>" in html  # knowledge layer badges
        assert f"/documents/{doc_id}?fragment=" in html  # citations link to the act
        assert "Использовано фрагментов:" in html

        operations = html.split('id="operations"')[1].split('id="decisions"')[0]
        unverified = html.split('id="unverified"')[1].split('id="coverage"')[0]
        assert HALLUCINATION not in operations  # not a fact
        assert HALLUCINATION in unverified and "операция" in unverified

        # Model text is escaped in the page and in the diagram.
        assert "<script>alert" not in html
        assert "&lt;script&gt;alert(&#39;op&#39;)&lt;/script&gt;" in html
        assert "#lt;script#gt;" in html


async def test_incomplete_banner_and_unknown_process(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        _, run_id = await env.analysed()
        run = await env.state.repos.runs.get(run_id)
        failed = FailedItem(stage=StageName.DETAIL, process_key="proc_2", error_code="llm_timeout")
        incomplete = Completeness(complete=False, failed_items=[failed])
        await env.state.repos.runs.update(run.model_copy(update={"completeness": incomplete}))
        assert "Анализ неполный" in (await env.client.get(f"/analyses/{run_id}/processes")).text
        status = (await env.client.get(f"/analyses/{run_id}")).text
        assert "Анализ неполный" in status and "proc_2" in status
        other = await env.client.get(f"/analyses/{run_id}/processes", params={"process": "x"})
        assert other.status_code == 200  # falls back to the first process


async def test_errors_are_html_pages(make_env: Callable[..., Any]) -> None:
    async for env in make_env(llm_data_policy="disabled"):
        doc_id = await env.upload()
        response = await env.client.post(f"/documents/{doc_id}/analyses")
        assert response.status_code == 503 and "text/html" in response.headers["content-type"]
        assert (
            "LLM не настроена" in response.text
            and f'href="/documents/{doc_id}#runs"' in response.text
        )
        missing = await env.client.get("/analyses/run_X/processes")
        assert missing.status_code == 404 and "Анализ не найден" in missing.text
