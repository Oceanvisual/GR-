"""Priority configuration API and the ranked table (api.md, FR-060…FR-065, Q7, T049)."""

import json
from collections.abc import Callable
from typing import Any

import httpx
from test_analysis_e2e import Env
from test_analysis_e2e import make_env as make_env

from gr.domain.enums import StageName
from gr.services.results import PRIORITY_ENTITY

Json = dict[str, Any]
CRITERIA = ("effect", "feasibility", "data_availability", "integration", "risk", "duration")


def ratings(**levels: str) -> Json:
    names = ("feasibility", "data_availability", "integration_complexity", "legal_risk",
             "operational_risk")  # fmt: skip
    found: Json = {name: {"level": levels.get(name, "medium"), "justification": "x"}
                   for name in names}  # fmt: skip
    found["duration"] = {"bucket": "m3_6", "justification": "x"}
    return found


async def analyse(env: Env) -> str:
    """Four initiatives on one operation, each on its own finding (so complementary)."""
    doc_id = await env.upload()
    template = json.loads(await env.answer(doc_id, "opp_correct"))
    (finding,) = template["findings"]
    (initiative,) = template["initiatives"]
    plans = {
        "i1": ratings(feasibility="very_high", data_availability="very_low"),
        "i2": ratings(feasibility="very_low", data_availability="very_high"),
        "i3": ratings(legal_risk="very_high"),  # blocked by the legal gate
        "i4": ratings(),
    }
    template["findings"] = [finding | {"key": f"f{n}"} for n in range(1, 5)]
    template["initiatives"] = [
        initiative | {"key": key, "title": f"Инициатива {key}", "finding_keys": [f"f{n}"],
                      "ratings": plan}
        for n, (key, plan) in enumerate(plans.items(), 1)
    ]  # fmt: skip
    env.llm.add(StageName.DISCOVER, await env.answer(doc_id, "discover_correct"))
    env.llm.add(StageName.DETAIL, await env.answer(doc_id, "detail_simple"))
    env.llm.add(StageName.OPPORTUNITIES, template)
    run_id: str = (await env.client.post(f"/api/v1/documents/{doc_id}/analyses")).json()["id"]
    assert (await env.finish(run_id))["status"] == "succeeded"
    return run_id


async def table(env: Env, run_id: str) -> httpx.Response:
    response = await env.client.get(f"/api/v1/analyses/{run_id}/initiatives?scenario=base")
    assert response.status_code == 200, response.text
    return response


def order(found: Json) -> list[str]:
    return [row["initiative"]["title"].removeprefix("Инициатива ") for row in found["rows"]]


async def put(env: Env, run_id: str, config: Json, version: str | None) -> httpx.Response:
    headers = {} if version is None else {"If-Match": version}
    return await env.client.put(
        f"/api/v1/analyses/{run_id}/priority-config", json=config, headers=headers
    )


def only(config: Json, criterion: str) -> Json:
    return config | {"weights": {c: (1 if c == criterion else 0) for c in CRITERIA}}


async def test_q7_identical_reads_and_weights_that_come_back(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        calls = len(env.llm.calls)
        first, second = await table(env, run_id), await table(env, run_id)
        assert first.content == second.content  # SC-006: byte for byte
        original = first.json()
        assert original["priority_config_version"] == 1
        assert order(original)[-1] == "i3"  # blocked: last, without a rank, with its reason
        blocked = original["rows"][-1]
        assert blocked["priority"]["rank"] is None
        assert blocked["priority"]["group"] == "not_recommended"
        assert blocked["priority"]["group_rule"] == 1
        assert blocked["initiative"]["gate"]["reasons"]

        response = await env.client.get(f"/api/v1/analyses/{run_id}/priority-config")
        assert response.headers["ETag"] == '"1"'
        defaults = response.json()
        feasible = await put(env, run_id, only(defaults, "feasibility"), '"1"')
        assert feasible.status_code == 200, feasible.text
        assert feasible.headers["ETag"] == '"2"' and feasible.json()["version"] == 2
        assert order(feasible.json()["initiatives"])[0] == "i1"
        data = await put(env, run_id, only(defaults, "data_availability"), '"2"')
        assert order(data.json()["initiatives"])[0] == "i2"
        restored = await put(env, run_id, defaults, '"3"')
        assert restored.status_code == 200 and restored.json()["version"] == 4
        back = (await table(env, run_id)).json()
        assert order(back) == order(original)
        scores = [(r["priority"]["score"], r["priority"]["rank"]) for r in back["rows"]]
        assert scores == [(r["priority"]["score"], r["priority"]["rank"]) for r in original["rows"]]
        assert len(env.llm.calls) == calls  # no LLM call to re-rank


async def test_versions_conflicts_validation_and_history(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        defaults = (await env.client.get(f"/api/v1/analyses/{run_id}/priority-config")).json()
        same = await put(env, run_id, defaults, '"1"')  # nothing changes: no new version
        assert same.status_code == 200 and same.json()["version"] == 1
        assert await env.state.repos.priority_configs.versions(run_id) == [1]

        changed = defaults | {"cutoff_score": "20", "weights": defaults["weights"] | {"risk": "40"}}
        assert (await put(env, run_id, changed, '"1"')).status_code == 200
        stale = await put(env, run_id, changed | {"cutoff_score": "10"}, '"1"')
        assert stale.status_code == 409
        assert stale.json()["error"]["details"]["current_version"] == 2

        zero = defaults | {"weights": dict.fromkeys(CRITERIA, 0)}
        refused = await put(env, run_id, zero, '"2"')
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "validation_error"
        assert (await put(env, run_id, defaults, None)).status_code == 400  # If-Match needed
        assert await env.state.repos.priority_configs.versions(run_id) == [1, 2]

        (event,) = await env.state.repos.reviews.list_for_run(run_id, PRIORITY_ENTITY)
        assert event.entity_type == "priority_config" and event.action == "edit"
        assert set(event.patch or {}) == {"cutoff_score", "weights"}
        assert event.previous is not None and event.previous["cutoff_score"] == "35"


async def test_portfolio_and_breakdown_reach_the_api(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        found = (await table(env, run_id)).json()
        assert found["portfolio"] is not None
        economics = (
            await env.client.get(f"/api/v1/analyses/{run_id}/economics?scenario=optimistic")
        ).json()
        assert economics["scenario"] == "optimistic" and economics["portfolio"] is not None
        key = next(r["initiative"]["id"] for r in found["rows"] if r["priority"]["rank"] == 1)
        card = (await env.client.get(f"/api/v1/analyses/{run_id}/initiatives/{key}")).json()
        priority = card["priority"]
        assert set(priority["criteria"]) == set(CRITERIA) and priority["rank"] == 1
        contributions = sum(float(c["contribution"]) for c in priority["criteria"].values())
        assert round(contributions, 1) == float(priority["score"])  # the breakdown adds up
        assert card["priority_config_version"] == 1
        assert card["in_portfolio"] == (card["excluded_reason"] is None)


async def test_weights_on_the_settings_screen(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        page = (await env.client.get(f"/analyses/{run_id}/settings")).text
        assert "Веса и пороги приоритизации" in page and 'name="w:effect"' in page
        form = {f"w:{c}": "1" for c in CRITERIA} | {"priority_version": "1"}
        form |= {f"lv:{k}": v for k, v in (("very_low", "0"), ("low", "0,25"), ("medium", "0,5"),
                                           ("high", "0,75"), ("very_high", "1"))}  # fmt: skip
        form |= {f"du:{k}": v for k, v in (("lt_3m", "1"), ("m3_6", "0,7"), ("m6_12", "0,4"),
                                           ("gt_12m", "0,1"))}  # fmt: skip
        form |= {"effect_ref": "5 000 000", "tech_guard_penalty": "0,25", "cutoff_score": "35",
                 "ease_threshold": "0,6", "effect_threshold": "0,4"}  # fmt: skip
        htmx = {"HX-Request": "true"}
        url = f"/analyses/{run_id}/settings/priority"
        zero = form | {f"w:{c}": "0" for c in CRITERIA}
        refused = (await env.client.post(url, data=zero, headers=htmx)).text
        assert "Веса — неотрицательные числа с суммой больше 0." in refused
        saved = await env.client.post(url, data=form, headers=htmx)
        assert saved.status_code == 200 and "Сохранено: версия 2" in saved.text
        assert saved.text.startswith('<div id="priority">')
        assert await env.state.repos.priority_configs.versions(run_id) == [1, 2]
