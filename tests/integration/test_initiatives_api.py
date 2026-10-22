"""US3 end to end: S6/S7, API, HTML evidence, filtering and partial output banners."""

import json
from collections.abc import Callable
from typing import Any

from test_analysis_e2e import Env
from test_analysis_e2e import make_env as make_env

from gr.domain.enums import StageName


async def analyse(env: Env, reply: str = "opp_correct", *, hostile: bool = False) -> str:
    doc_id = await env.upload()
    env.llm.add(StageName.DISCOVER, await env.answer(doc_id, "discover_correct"))
    env.llm.add(StageName.DETAIL, await env.answer(doc_id, "detail_simple"))
    raw = json.loads(await env.answer(doc_id, reply))
    if hostile:
        raw["initiatives"][0]["title"] = '<script>alert("xss")</script>'
    env.llm.add(StageName.OPPORTUNITIES, raw)
    response = await env.client.post(f"/api/v1/documents/{doc_id}/analyses")
    run_id: str = response.json()["id"]
    done = await env.finish(run_id)
    assert done["status"] == "succeeded", done
    assert all(s["status"] == "succeeded" for s in done["stages"])
    return run_id


async def test_initiatives_are_readable_with_citations_and_without_more_llm(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        calls = len(env.llm.calls)
        response = await env.client.get(f"/api/v1/analyses/{run_id}/initiatives")
        assert response.status_code == 200, response.text
        table = response.json()
        (row,) = table["rows"]
        assert row["initiative"]["tech_guard"]["requires_justification"] is False
        assert row["finding_patterns"] == ["duplicate_checks"]
        key = row["initiative"]["id"]
        card = await env.client.get(f"/api/v1/analyses/{run_id}/initiatives/{key}")
        assert card.status_code == 200
        assert card.json()["operations"][0]["action"]["citations"][0]["quote"]
        assert card.json()["findings"][0]["layer"] == "hypothesis"
        assert len(env.llm.calls) == calls
        assert not (
            await env.client.get(f"/api/v1/analyses/{run_id}/initiatives?process_id=missing")
        ).json()["rows"]
        assert not (
            await env.client.get(f"/api/v1/analyses/{run_id}/initiatives?gate=blocked")
        ).json()["rows"]
        assert (
            await env.client.get(f"/api/v1/analyses/{run_id}/initiatives?gate=wrong")
        ).status_code == 400
        assert (
            await env.client.get(f"/api/v1/analyses/{run_id}/initiatives/unknown")
        ).status_code == 404
        assert (await env.client.get("/api/v1/analyses/unknown/initiatives")).status_code == 404


async def test_html_table_detail_and_filters_are_safe(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env, hostile=True)
        table = await env.client.get(f"/analyses/{run_id}/initiatives")
        assert table.status_code == 200 and "Гипотеза" in table.text
        assert "не сокращение бюджета" in table.text
        assert '<script>alert("xss")</script>' not in table.text
        assert "&lt;script&gt;" in table.text
        assert (
            await env.client.get(f"/analyses/{run_id}/initiatives?process_id=&gate=")
        ).status_code == 200
        card = await env.client.get(f"/analyses/{run_id}/initiatives/ini_proc_1_1")
        assert card.status_code == 200
        assert "Операции и нормативные основания" in card.text
        assert "проверяет комплектность документов" in card.text
        assert "Рассмотренные альтернативы" in card.text
        assert "Балл: сумма вкладов" in card.text  # the priority breakdown (FR-070)
        assert "Срок внедрения: 3–6 месяцев" in card.text and "m3_6" not in card.text
        assert "<li>Нет данных</li>" in card.text  # no risks given: FR-071
        assert "Связанных инициатив нет" in card.text
        assert (await env.client.get(f"/analyses/{run_id}/initiatives/unknown")).status_code == 404


async def test_invalid_links_make_partial_output_visible(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env, "opp_unknown_ids")
        table = (await env.client.get(f"/api/v1/analyses/{run_id}/initiatives")).json()
        assert not table["rows"]
        assert any("Анализ неполный" in b for b in table["banners"])
        assert any(i["code"] == "unknown_operation" for i in table["issues"])
        assert "Анализ неполный" in (await env.client.get(f"/analyses/{run_id}/initiatives")).text


async def test_explicit_no_change_is_visible(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env, "opp_no_change")
        table = (await env.client.get(f"/api/v1/analyses/{run_id}/initiatives")).json()
        assert not table["rows"] and len(table["no_change"]) == 1
        html = (await env.client.get(f"/analyses/{run_id}/initiatives")).text
        assert "Оптимизация не требуется" in html
        processes = (await env.client.get(f"/api/v1/analyses/{run_id}/processes")).json()
        name = processes["processes"][0]["name"]["value"]
        assert f"<strong>{name}</strong>" in html  # the process by name, not by id
