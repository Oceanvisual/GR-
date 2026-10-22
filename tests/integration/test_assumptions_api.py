"""Assumptions and economics API (api.md, FR-056, FR-057, SC-005, T045)."""

import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from test_analysis_e2e import Env
from test_analysis_e2e import make_env as make_env

from gr.domain.enums import StageName
from gr.services.results import ASSUMPTIONS_ENTITY, DEFAULTS_BANNER

Json = dict[str, Any]


async def analyse(env: Env, opportunities: Json | None = None) -> str:
    doc_id = await env.upload()
    env.llm.add(StageName.DISCOVER, await env.answer(doc_id, "discover_correct"))
    env.llm.add(StageName.DETAIL, await env.answer(doc_id, "detail_simple"))
    env.llm.add(
        StageName.OPPORTUNITIES,
        opportunities or json.loads(await env.answer(doc_id, "opp_correct")),
    )
    response = await env.client.post(f"/api/v1/documents/{doc_id}/analyses")
    run_id: str = response.json()["id"]
    done = await env.finish(run_id)
    assert done["status"] == "succeeded", done
    return run_id


async def table(env: Env, run_id: str, scenario: str = "base") -> Json:
    response = await env.client.get(f"/api/v1/analyses/{run_id}/initiatives?scenario={scenario}")
    assert response.status_code == 200, response.text
    found: Json = response.json()
    return found


async def patch(
    env: Env, run_id: str, changes: list[Json], version: str | None, **body: Any
) -> httpx.Response:
    headers = {} if version is None else {"If-Match": version}
    return await env.client.patch(
        f"/api/v1/analyses/{run_id}/assumptions",
        json={"changes": changes, **body},
        headers=headers,
    )


def hours(found: Json) -> list[Decimal]:
    (row,) = found["rows"]
    return [Decimal(h) for h in row["economics"]["solo"]["hours"]]


def triple(conservative: str, base: str, optimistic: str) -> Json:
    return {"conservative": conservative, "base": base, "optimistic": optimistic}


async def test_s7_creates_the_first_version_and_reading_needs_no_llm(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        assert await env.state.repos.assumptions.versions(run_id) == [1]
        calls = len(env.llm.calls)

        response = await env.client.get(f"/api/v1/analyses/{run_id}/assumptions")
        assert response.status_code == 200 and response.headers["ETag"] == '"1"'
        current = response.json()
        assert current["version"] == 1
        assert {i["source"] for i in current["items"]} == {"default_profile"}

        base = await table(env, run_id)
        assert base["assumptions_version"] == 1
        assert len(hours(base)) == 3  # the profile horizon
        assert DEFAULTS_BANNER in base["banners"]
        assert any("Объём процесса" in b for b in base["banners"])
        assert hours(await table(env, run_id, "conservative")) != hours(base)

        economics = (
            await env.client.get(f"/api/v1/analyses/{run_id}/economics?scenario=optimistic")
        ).json()
        assert economics["scenario"] == "optimistic" and economics["layer"] == "assumption"
        (row,) = base["rows"]
        key = row["initiative"]["id"]
        card = (await env.client.get(f"/api/v1/analyses/{run_id}/initiatives/{key}")).json()
        assert set(card["economics"]) == {"conservative", "base", "optimistic"}
        assert card["economics"]["base"] == row["economics"]["solo"]
        used = {a["key"] for a in card["assumptions"]}
        assert "hourly_cost" in used and any(k.endswith(".reduction_share") for k in used)
        assert len(env.llm.calls) == calls  # SC-005: no LLM call to compute economics


async def test_patch_saves_a_version_logs_it_and_returns_the_recomputed_table(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        before = await table(env, run_id)
        process = before["rows"][0]["initiative"]["process_id"]
        volume = f"proc.{process}.volume_per_year"
        calls = len(env.llm.calls)

        response = await patch(
            env,
            run_id,
            [{"key": volume, "values": triple("1000", "2000", "3000"), "note": "Журнал 2025"}],
            "1",  # a bare version number is accepted as well as "1" and W/"1"
            comment="Объём из журнала регистрации",
        )
        assert response.status_code == 200, response.text
        assert response.headers["ETag"] == '"2"'
        update = response.json()
        assert update["assumption_set"]["version"] == 2
        item = next(i for i in update["assumption_set"]["items"] if i["key"] == volume)
        assert item["source"] == "user" and not item["requires_input"]
        assert item["note"] == "Журнал 2025"
        # Hours are proportional to the volume: 1 000 → 2 000 doubles them.
        assert hours(update["initiatives"]) == [h * 2 for h in hours(before)]
        assert update["initiatives"]["assumptions_version"] == 2
        assert not any("Объём процесса" in b for b in update["initiatives"]["banners"])
        assert len(env.llm.calls) == calls

        (event,) = await env.state.repos.reviews.list_for_run(run_id, ASSUMPTIONS_ENTITY)
        assert event.entity_type == "assumption_set" and event.action == "edit"
        assert event.comment == "Объём из журнала регистрации"
        assert event.patch is not None and event.patch[volume]["values"]["base"] == "2000"
        assert event.previous is not None and event.previous[volume]["values"]["base"] == "1000"
        assert item["updated_by"] == event.user

        current = await env.client.get(f"/api/v1/analyses/{run_id}/assumptions")
        assert current.headers["ETag"] == '"2"'
        again = await patch(
            env, run_id, [{"key": volume, "note": "сверено"}], 'W/"2"'
        )  # fmt: skip
        assert again.status_code == 200 and again.headers["ETag"] == '"3"'


async def test_a_stale_version_is_a_conflict(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        change = [{"key": "hourly_cost", "values": triple("1300", "1300", "1300")}]
        assert (await patch(env, run_id, change, '"1"')).status_code == 200
        stale = await patch(env, run_id, change, '"1"')  # another tab, based on version 1
        assert stale.status_code == 409
        error = stale.json()["error"]
        assert error["code"] == "version_conflict" and error["details"]["current_version"] == 2
        assert await env.state.repos.assumptions.versions(run_id) == [1, 2]


async def test_invalid_values_are_listed_by_key_and_nothing_is_saved(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        response = await patch(
            env,
            run_id,
            [
                {"key": "overhead_ratio", "values": triple("0.3", "1.5", "0.3")},
                {"key": "hourly_cost", "values": triple("-1", "1200", "1200")},
                {"key": "horizon_years", "values": triple("2", "3", "3")},
                {"key": "op.ghost.time_hours", "values": triple("1", "1", "1")},
                {"key": "fte_hours_per_year", "values": triple("1", "1", "1")},
                {"key": "fte_hours_per_year", "values": triple("2", "2", "2")},
                {"key": "proc.ghost.hourly_cost_override", "values": triple("1", "1", "1")},
                {"key": "proc.x.volume_per_year"},
            ],
            '"1"',
        )
        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert error["details"]["keys"] == {
            "overhead_ratio": "доля должна быть от 0 до 1",
            "hourly_cost": "значения не могут быть отрицательными",
            "horizon_years": "горизонт одинаков во всех сценариях",
            "op.ghost.time_hours": "неизвестное допущение",
            "fte_hours_per_year": "ключ указан дважды",
            "proc.ghost.hourly_cost_override": "неизвестное допущение",
            "proc.x.volume_per_year": "нет изменений",
        }
        zero_year = [{"key": "fte_hours_per_year", "values": triple("0", "1970", "1970")}]
        refused = await patch(env, run_id, zero_year, '"1"')
        assert refused.status_code == 400
        assert list(refused.json()["error"]["details"]["keys"]) == ["fte_hours_per_year"]
        assert await env.state.repos.assumptions.versions(run_id) == [1]


async def test_if_match_is_required(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        change = [{"key": "hourly_cost", "values": triple("1", "1", "1")}]
        for header in (None, "latest"):
            response = await patch(env, run_id, change, header)
            assert response.status_code == 400 and "If-Match" in response.text


async def test_scenarios_out_of_order_are_warned_not_refused(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        key = (await table(env, run_id))["rows"][0]["initiative"]["id"]
        capex = f"ini.{key}.capex"
        response = await patch(
            env, run_id, [{"key": capex, "values": triple("1", "2", "3")}], '"1"'
        )
        assert response.status_code == 200, response.text
        assert any(capex in w and "не по порядку" in w for w in response.json()["warnings"])


async def test_a_process_rate_can_be_set(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        before = await table(env, run_id)
        process = before["rows"][0]["initiative"]["process_id"]
        rate = f"proc.{process}.hourly_cost_override"
        doubled = [{"key": rate, "values": triple("2400", "2400", "2400")}]
        response = await patch(env, run_id, doubled, '"1"')
        assert response.status_code == 200, response.text
        (item,) = [i for i in response.json()["assumption_set"]["items"] if i["key"] == rate]
        assert (item["scope"], item["target_id"], item["source"]) == ("process", process, "user")
        # The profile rate is 1 200: the money doubles with the hours unchanged.
        after = response.json()["initiatives"]
        assert hours(after) == hours(before)
        money = [Decimal(m) for m in before["rows"][0]["economics"]["solo"]["money"]]
        new_money = [Decimal(m) for m in after["rows"][0]["economics"]["solo"]["money"]]
        assert new_money == [m * 2 for m in money]


@pytest.mark.slow
async def test_recalculation_of_100_initiatives_takes_under_two_seconds(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        doc_id = await env.upload()
        template = json.loads(await env.answer(doc_id, "opp_correct"))
        (initiative,) = template["initiatives"]
        template["initiatives"] = [initiative | {"key": f"i{n}"} for n in range(1, 101)]
        env.llm.add(StageName.DISCOVER, await env.answer(doc_id, "discover_correct"))
        env.llm.add(StageName.DETAIL, await env.answer(doc_id, "detail_simple"))
        env.llm.add(StageName.OPPORTUNITIES, template)
        response = await env.client.post(f"/api/v1/documents/{doc_id}/analyses")
        run_id = response.json()["id"]
        assert (await env.finish(run_id))["status"] == "succeeded"
        assert len((await table(env, run_id))["rows"]) == 100

        started = time.perf_counter()
        change = [{"key": "hourly_cost", "values": triple("1500", "1500", "1500")}]
        response = await patch(env, run_id, change, '"1"')
        elapsed = time.perf_counter() - started
        assert response.status_code == 200, response.text
        assert len(response.json()["initiatives"]["rows"]) == 100
        assert elapsed <= 2.0, f"{elapsed:.2f} s"  # SC-005
