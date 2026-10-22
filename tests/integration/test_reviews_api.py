"""Expert review API and the decisions in every read (api.md, FR-080…FR-083, Q8, T051)."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from test_analysis_e2e import Env
from test_analysis_e2e import make_env as make_env

from gr.domain.enums import StageName
from gr.domain.review import ReviewEvent

Json = dict[str, Any]
REJECTED = "Отклонена экспертом."


async def analyse(env: Env) -> str:
    """Two initiatives on different operations of one process, both in the portfolio."""
    doc_id = await env.upload()
    template = json.loads(await env.answer(doc_id, "opp_correct"))
    (finding,) = template["findings"]
    (initiative,) = template["initiatives"]
    (potential,) = initiative["reduction_potential"]
    plans = {"i1": "proc_1.op_2", "i2": "proc_1.op_1"}
    template["findings"] = [
        finding | {"key": f"f_{key}", "operation_ids": [op]} for key, op in plans.items()
    ]
    template["initiatives"] = [
        initiative | {"key": key, "title": f"Инициатива {key}", "finding_keys": [f"f_{key}"],
                      "operation_ids": [op],
                      "reduction_potential": [potential | {"operation_id": op}]}
        for key, op in plans.items()
    ]  # fmt: skip
    env.llm.add(StageName.DISCOVER, await env.answer(doc_id, "discover_correct"))
    env.llm.add(StageName.DETAIL, await env.answer(doc_id, "detail_simple"))
    env.llm.add(StageName.OPPORTUNITIES, template)
    run_id: str = (await env.client.post(f"/api/v1/documents/{doc_id}/analyses")).json()["id"]
    assert (await env.finish(run_id))["status"] == "succeeded"
    volume = {"conservative": "100000", "base": "100000", "optimistic": "100000"}
    changes = [{"key": "proc.proc_1.volume_per_year", "values": volume}]
    saved = await env.client.patch(
        f"/api/v1/analyses/{run_id}/assumptions", json={"changes": changes},
        headers={"If-Match": '"1"'},
    )  # fmt: skip
    assert saved.status_code == 200, saved.text
    return run_id


async def table(env: Env, run_id: str, query: str = "") -> Json:
    response = await env.client.get(f"/api/v1/analyses/{run_id}/initiatives{query}")
    assert response.status_code == 200, response.text
    found: Json = response.json()
    return found


def ids(found: Json) -> dict[str, str]:
    """Title suffix → initiative id."""
    return {
        row["initiative"]["title"].removeprefix("Инициатива "): row["initiative"]["id"]
        for row in found["rows"]
    }


async def review(env: Env, run_id: str, **body: Any) -> httpx.Response:
    return await env.client.post(f"/api/v1/analyses/{run_id}/reviews", json=body)


async def history(env: Env, run_id: str, query: str = "") -> list[Json]:
    response = await env.client.get(f"/api/v1/analyses/{run_id}/reviews{query}")
    assert response.status_code == 200, response.text
    found: list[Json] = response.json()
    return found


def operation(processes: Json, key: str) -> Json:
    found: Json = next(o for p in processes["processes"] for o in p["operations"] if o["id"] == key)
    return found


async def test_q8_reject_and_correct_a_deadline_then_restart(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        before = await table(env, run_id)
        key = ids(before)
        assert sorted(before["portfolio"]["initiative_ids"]) == sorted(key.values())
        calls = len(env.llm.calls)

        unexplained = await review(env, run_id, entity_type="initiative", entity_id=key["i1"],
                                   action="reject", comment="  ")  # fmt: skip
        assert unexplained.status_code == 400
        assert "комментари" in unexplained.json()["error"]["message"]
        reason = "Перечень документов меняется"
        rejected = await review(env, run_id, entity_type="initiative", entity_id=key["i1"],
                                action="reject", comment=reason)  # fmt: skip
        assert rejected.status_code == 201, rejected.text
        assert rejected.json()["entity"]["review"]["status"] == "rejected"
        assert rejected.json()["event"]["comment"] == reason

        after = await table(env, run_id)
        assert after["portfolio"]["initiative_ids"] == [key["i2"]]  # gone from the portfolio
        assert list(ids(after).values()) == [key["i2"]]  # and from the table by default
        shown = await table(env, run_id, "?include_rejected=true")
        out = next(r for r in shown["rows"] if r["initiative"]["id"] == key["i1"])
        assert out["excluded_reason"] == REJECTED
        assert out["economics"] is None and out["priority"] is None
        card = (await env.client.get(f"/api/v1/analyses/{run_id}/initiatives/{key['i1']}")).json()
        assert card["excluded_reason"] == REJECTED and not card["in_portfolio"]
        assert [e["action"] for e in card["history"]] == ["reject"]

        deadline = {"value": 2, "unit": "working_days", "raw": "два рабочих дня"}
        corrected = await review(env, run_id, entity_type="operation", entity_id="proc_1.op_1",
                                 action="edit", patch={"deadline": deadline})  # fmt: skip
        assert corrected.status_code == 201, corrected.text
        entity = corrected.json()["entity"]
        assert (
            entity["deadline"]["value"] == 2
            and entity["deadline"]["raw"]["value"] == deadline["raw"]
        )
        assert entity["review"] == {
            "status": "edited",
            "last_event_id": corrected.json()["event"]["id"],
            "edited_fields": ["deadline"],
        }  # the «исправлено» mark
        assert corrected.json()["event"]["previous"] == {"deadline": None}
        assert corrected.json()["stale_initiative_ids"] == [key["i2"]]  # built on that operation

        processes = (await env.client.get(f"/api/v1/analyses/{run_id}/processes")).json()
        assert operation(processes, "proc_1.op_1")["deadline"]["value"] == 2
        stored = await env.state.repos.stage_outputs.get(run_id, StageName.VALIDATE)
        assert stored is not None  # the output of S5 keeps the model's value (FR-081)
        assert operation(stored, "proc_1.op_1")["deadline"] is None
        single = await env.client.get(f"/api/v1/analyses/{run_id}/processes/proc_1")
        assert single.json()["operations"][0]["deadline"]["value"] == 2

        stale = await table(env, run_id)
        (row,) = stale["rows"]
        assert row["initiative"]["stale"] is True
        assert any("требует пересмотра" in banner for banner in stale["banners"])

        log = await history(env, run_id)
        assert [e["action"] for e in log] == ["edit", "reject", "edit"]  # the latest first
        assert [e["entity_type"] for e in log] == ["operation", "initiative", "assumption_set"]
        assert [e["entity_id"] for e in await history(env, run_id, f"?entity_id={key['i1']}")] == [
            key["i1"]
        ]
        assert len(await history(env, run_id, "?entity_type=operation")) == 1
        assert len(await history(env, run_id, "?user=nobody")) == 0
        assert len(env.llm.calls) == calls  # decisions never call the LLM
        reads = {
            "table": await table(env, run_id, "?include_rejected=true"),
            "processes": processes,
            "log": log,
        }

    async for env in make_env():  # a new application on the same database
        assert await table(env, run_id, "?include_rejected=true") == reads["table"]
        assert (await env.client.get(f"/api/v1/analyses/{run_id}/processes")).json() == reads[
            "processes"
        ]
        assert await history(env, run_id) == reads["log"]


async def test_decisions_that_cannot_apply_are_refused(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        key = ids(await table(env, run_id))
        logged = len(await history(env, run_id))
        confirm: Json = {
            "entity_type": "operation",
            "entity_id": "proc_1.op_2",
            "action": "confirm",
        }
        assert (await review(env, run_id, **confirm)).status_code == 201
        refused: dict[str, Json] = {
            "confirmed twice": confirm,
            "field outside the whitelist": {**confirm, "action": "edit", "patch": {"seq": 5}},
            "value of a wrong type": {**confirm, "action": "edit",
                                      "patch": {"operation_type": "unknown"}},
            "required value removed": {**confirm, "action": "edit", "patch": {"action": None}},
            "gate of an operation": {**confirm, "action": "set_gate",
                                     "patch": {"status": "blocked"}},
            "unknown gate": {"entity_type": "initiative", "entity_id": key["i1"],
                             "action": "set_gate", "patch": {"status": "maybe"}},
            "edit without a patch": {**confirm, "action": "edit"},
            "patch of a confirm": {"entity_type": "process", "entity_id": "proc_1",
                                   "action": "confirm", "patch": {"name": "x"}},
            "versioned entity": {"entity_type": "assumption_set", "entity_id": "assumptions",
                                 "action": "edit", "patch": {"x": 1}},
            "relation to itself": {"entity_type": "relation", "action": "edit",
                                   "entity_id": f"{key['i1']}|{key['i1']}",
                                   "patch": {"relation": "alternative"}},
            "unknown relation": {"entity_type": "relation", "action": "edit",
                                 "entity_id": f"{key['i1']}|{key['i2']}",
                                 "patch": {"relation": "rival"}},
        }  # fmt: skip
        for case, body in refused.items():
            response = await review(env, run_id, **body)
            assert response.status_code == 400, (case, response.text)
            assert response.json()["error"]["code"] == "validation_error", case
        missing = {**confirm, "entity_id": "proc_1.op_9"}
        assert (await review(env, run_id, **missing)).status_code == 404
        assert (await review(env, "run_unknown", **confirm)).status_code == 404
        assert (await env.client.get("/api/v1/analyses/run_unknown/reviews")).status_code == 404
        assert len(await history(env, run_id)) == logged + 1  # only the first confirm
        # The log still applies: every read works.
        assert (await env.client.get(f"/api/v1/analyses/{run_id}/processes")).status_code == 200
        assert (await env.client.get(f"/analyses/{run_id}/processes")).status_code == 200


async def test_quotes_of_the_expert_are_checked_when_saved(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        url = "body/III/3.2/3.2.1"
        quote = "регистрирует его в системе электронного документооборота"
        cited = await review(
            env, run_id, entity_type="operation", entity_id="proc_1.op_1", action="edit",
            patch={"action": {"value": "регистрирует заявление в СЭД",
                              "citation": {"fragment_id": url, "quote": quote}}},
        )  # fmt: skip
        assert cited.status_code == 201, cited.text
        action = cited.json()["entity"]["action"]
        assert action["layer"] == "fact" and action["value"] == "регистрирует заявление в СЭД"
        (citation,) = action["citations"]
        assert citation["fragment_id"] == url and citation["grounding"]["status"] == "verified"

        moved = await review(
            env, run_id, entity_type="process", entity_id="proc_1", action="edit",
            patch={"result": {"value": "пропуск выдан",
                              "citation": {"fragment_id": "body/II/2.1",
                                           "quote": "оформляет пропуск и выдаёт его заявителю"}}},
        )  # fmt: skip
        assert moved.status_code == 201, moved.text
        (relocated,) = moved.json()["entity"]["result"]["citations"]
        assert relocated["fragment_id"] == "body/III/3.4/3.4.1"
        assert relocated["grounding"]["status"] == "relocated"

        fiction = {"fragment_id": url, "quote": "проверку выполняет робот без участия людей"}
        invented = await review(
            env, run_id, entity_type="operation", entity_id="proc_1.op_2", action="edit",
            patch={"performer": {"value": "робот", "citation": fiction}},
        )  # fmt: skip
        assert invented.status_code == 400
        assert set(invented.json()["error"]["details"]["fields"]) == {"performer"}
        bad_shape = await review(
            env, run_id, entity_type="operation", entity_id="proc_1.op_2", action="edit",
            patch={"performer": {"value": "x", "citation": {"page": "3", "quote": quote}}},
        )  # fmt: skip
        assert bad_shape.status_code == 400

        plain = await review(env, run_id, entity_type="operation", entity_id="proc_1.op_2",
                             action="edit", patch={"performer": "специалист МФЦ"})  # fmt: skip
        assert plain.status_code == 201, plain.text
        assert plain.json()["entity"]["performer"]["layer"] == "interpretation"
        assert plain.json()["event"]["previous"]["performer"] is None

        # No fragment named: the act is searched, the paragraph holding the quote is the basis.
        anywhere = await review(
            env, run_id, entity_type="operation", entity_id="proc_1.op_2", action="edit",
            patch={"performer": {"value": "специалист отдела делопроизводства",
                                 "citation": {"quote": "Специалист отдела делопроизводства "
                                                       "проверяет комплектность документов"}}},
        )  # fmt: skip
        assert anywhere.status_code == 201, anywhere.text
        (basis,) = anywhere.json()["entity"]["performer"]["citations"]
        assert basis["fragment_id"] == "body/III/3.2/3.2.2"
        assert basis["grounding"]["status"] == "verified"


async def test_gate_relation_reopen_and_a_rejected_process(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        key = ids(await table(env, run_id))
        pair = "|".join(sorted(key.values(), reverse=True))  # any order is accepted
        related = await review(env, run_id, entity_type="relation", entity_id=pair,
                               action="edit", patch={"relation": "alternative"})  # fmt: skip
        assert related.status_code == 201, related.text
        assert related.json()["entity"] == {
            "initiative_ids": sorted(key.values()),
            "relation": "alternative",
        }
        assert related.json()["event"]["entity_id"] == "|".join(sorted(key.values()))
        assert related.json()["event"]["previous"] == {"relation": None}
        alternatives = await table(env, run_id)
        assert len(alternatives["portfolio"]["initiative_ids"]) == 1  # the better one only
        card = (await env.client.get(f"/api/v1/analyses/{run_id}/initiatives/{key['i1']}")).json()
        assert card["initiative"]["relations"] == [
            {"initiative_id": key["i2"], "relation": "alternative", "source": "expert"}
        ]
        back = await review(env, run_id, entity_type="relation", entity_id=pair,
                            action="edit", patch={"relation": "complementary"})  # fmt: skip
        assert back.json()["event"]["previous"] == {"relation": "alternative"}
        assert len((await table(env, run_id))["portfolio"]["initiative_ids"]) == 2

        gate = await review(env, run_id, entity_type="initiative", entity_id=key["i2"],
                            action="set_gate", patch={"status": "blocked"},
                            comment="Нужна экспертиза ведомства")  # fmt: skip
        assert gate.status_code == 201, gate.text
        assert gate.json()["entity"]["gate"]["status"] == "blocked"
        blocked = await table(env, run_id)
        assert blocked["portfolio"]["initiative_ids"] == [key["i1"]]
        row = next(r for r in blocked["rows"] if r["initiative"]["id"] == key["i2"])
        assert row["priority"]["rank"] is None and row["priority"]["group_rule"] == 1

        reject = {"entity_type": "process", "entity_id": "proc_1"}
        assert (
            await review(env, run_id, **reject, action="reject", comment="Не процесс")
        ).status_code == 201
        gone = await table(env, run_id)
        assert gone["portfolio"]["initiative_ids"] == []
        assert {r["excluded_reason"] for r in gone["rows"]} == {
            "Все операции инициативы отклонены экспертом."
        }
        assert all(r["initiative"]["stale"] for r in gone["rows"])
        for page in ("processes", "initiatives", "settings"):
            assert (await env.client.get(f"/analyses/{run_id}/{page}")).status_code == 200, page
        html = (await env.client.get(f"/analyses/{run_id}/initiatives")).text
        assert "Все операции инициативы отклонены экспертом." in html
        assert "Требует пересмотра" in html
        card_html = (await env.client.get(f"/analyses/{run_id}/initiatives/{key['i1']}")).text
        assert "Все операции инициативы отклонены экспертом. Эффект не рассчитывается." in card_html
        reopened = await review(env, run_id, **reject, action="reopen")
        assert (
            reopened.status_code == 201
            and reopened.json()["entity"]["review"]["status"] == "pending"
        )
        assert sorted((await table(env, run_id))["portfolio"]["initiative_ids"]) == [key["i1"]]


def delayed(write: Callable[[ReviewEvent], Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """A slow write: without the lock both requests would read the log before either writes."""

    async def slow(event: ReviewEvent) -> None:
        await asyncio.sleep(0.05)
        await write(event)

    return slow


async def test_simultaneous_decisions_are_written_one_at_a_time(
    make_env: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        repository = env.state.repos.reviews
        monkeypatch.setattr(repository, "add", delayed(repository.add))
        confirm = {"entity_type": "operation", "entity_id": "proc_1.op_1", "action": "confirm"}
        both = await asyncio.gather(review(env, run_id, **confirm), review(env, run_id, **confirm))
        # The second sees the first and is refused: confirmed → confirm is not in the diagram.
        assert sorted(r.status_code for r in both) == [201, 400]
        assert len(await history(env, run_id, "?entity_type=operation")) == 1
        assert (await env.client.get(f"/api/v1/analyses/{run_id}/processes")).status_code == 200
