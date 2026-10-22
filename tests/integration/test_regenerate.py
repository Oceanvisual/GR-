"""Regeneration of the initiatives of a process (FR-083, api.md, T052) on FakeLLM."""

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from test_analysis_e2e import Env
from test_analysis_e2e import make_env as make_env

from gr.domain.enums import StageName
from gr.extraction.render import FragmentAliases
from gr.llm.base import LLMTimeoutError
from gr.llm.fake import FakeCall
from gr.services.results import STALE_BANNER

Json = dict[str, Any]
Plans = dict[str, tuple[str, str]]  # initiative key → (operation, solution category)
CONTROL = {
    "key": "p2",
    "name": "Контроль за исполнением регламента",
    "trigger": "плановая проверка",
    "result": "результаты проверки",
    "core_refs": ["@п. 4.1@", "@п. 4.2@"],
    "related_refs": [],
    "evidence": {"text": "текущий контроль", "ref": "@п. 4.1@",
                 "quote": "Текущий контроль за соблюдением регламента осуществляет"},
    "parent_key": "",
}  # fmt: skip
CONTROL_DETAIL = {
    "process": {"name": {"text": "Контроль за исполнением регламента", "ref": "@п. 4.1@",
                         "quote": "Текущий контроль за соблюдением регламента осуществляет"}},
    "operations": [
        {"key": "o1", "name": "Текущий контроль",
         "action": {"text": "осуществляет текущий контроль", "ref": "@п. 4.1@",
                    "quote": "Текущий контроль за соблюдением регламента осуществляет"},
         "operation_type": "control"},
        {"key": "o2", "name": "Плановая проверка",
         "action": {"text": "проводит плановые проверки", "ref": "@п. 4.2@",
                    "quote": "Плановые проверки полноты и качества предоставления услуги"},
         "depends_on": ["o1"], "operation_type": "control"},
    ],
}  # fmt: skip
FIRST: dict[str, Plans] = {
    "proc_1": {"i1": ("proc_1.op_2", "classic_automation"),
               "i2": ("proc_1.op_1", "classic_automation")},
    "proc_2": {"k1": ("proc_2.op_2", "classic_automation")},
}  # fmt: skip
# j1 acts like i1 (same operation and category): it takes i1's values; j2 does not match i2.
SECOND: Plans = {"j1": ("proc_1.op_2", "classic_automation"), "j2": ("proc_1.op_1", "ai_nlp")}


async def labelled(env: Env, doc_id: str, data: Any) -> Any:
    """`@label@` → the alias of that fragment, as in the answer fixtures."""
    fragments = await env.state.repos.fragments.list_for_document(doc_id)
    aliases, by_label = FragmentAliases(fragments), {f.label: f.id for f in fragments}
    text = json.dumps(data, ensure_ascii=False)
    return json.loads(re.sub(r"@([^@]+)@", lambda m: aliases.alias(by_label[m.group(1)]), text))


def opportunities(template: Json, plans: Plans) -> Json:
    (finding,) = template["findings"]
    (initiative,) = template["initiatives"]
    (potential,) = initiative["reduction_potential"]
    return template | {
        "findings": [finding | {"key": f"f_{k}", "operation_ids": [op]}
                     for k, (op, _) in plans.items()],
        "initiatives": [
            initiative | {"key": k, "title": f"Инициатива {k}", "finding_keys": [f"f_{k}"],
                          "operation_ids": [op], "solution_category": category,
                          "reduction_potential": [potential | {"operation_id": op}]}
            for k, (op, category) in plans.items()
        ],
    }  # fmt: skip


def card_of(call: FakeCall) -> str:
    return call.user.split("<process_card>", 1)[1].split("</process_card>", 1)[0]


async def analyse(env: Env) -> tuple[str, str, Json]:
    """Two processes, two initiatives in the first one; returns the S6 template too."""
    doc_id = await env.upload()
    discover = json.loads(await env.answer(doc_id, "discover_correct"))
    discover["processes"].append(await labelled(env, doc_id, CONTROL))
    discover["non_process_refs"] = [r for r in discover["non_process_refs"] if "4." not in r]
    details: dict[str, Json] = {
        "simple": json.loads(await env.answer(doc_id, "detail_simple")),
        "control": await labelled(env, doc_id, CONTROL_DETAIL),
    }

    def detail(call: FakeCall) -> Json:
        return details["control" if "Контроль" in card_of(call) else "simple"]

    template = json.loads(await env.answer(doc_id, "opp_correct"))
    answers = {pid: opportunities(template, plans) for pid, plans in FIRST.items()}

    def first(call: FakeCall) -> Json:
        return next(a for pid, a in answers.items() if f'"{pid}.op_1"' in call.user)

    env.llm.add(StageName.DISCOVER, discover)
    env.llm.add(StageName.DETAIL, detail, detail)
    env.llm.add(StageName.OPPORTUNITIES, first, first)
    run_id: str = (await env.client.post(f"/api/v1/documents/{doc_id}/analyses")).json()["id"]
    done = await env.finish(run_id)
    assert done["status"] == "succeeded", done
    return run_id, doc_id, template


async def table(env: Env, run_id: str, query: str = "?include_rejected=true") -> Json:
    response = await env.client.get(f"/api/v1/analyses/{run_id}/initiatives{query}")
    assert response.status_code == 200, response.text
    found: Json = response.json()
    return found


def ids(found: Json) -> dict[str, str]:
    """Title suffix → initiative id."""
    return {
        r["initiative"]["title"].removeprefix("Инициатива "): r["initiative"]["id"]
        for r in found["rows"]
    }


def row(found: Json, key: str) -> Json:
    selected: Json = next(r for r in found["rows"] if r["initiative"]["id"] == key)
    return selected


async def review(env: Env, run_id: str, **body: Any) -> httpx.Response:
    return await env.client.post(f"/api/v1/analyses/{run_id}/reviews", json=body)


async def regenerate(env: Env, run_id: str, process_id: str = "proc_1") -> httpx.Response:
    return await env.client.post(
        f"/api/v1/analyses/{run_id}/processes/{process_id}/regenerate-initiatives"
    )


async def versions(env: Env, run_id: str, process_id: str = "proc_1") -> list[Json]:
    url = f"/api/v1/analyses/{run_id}/processes/{process_id}/initiative-versions"
    response = await env.client.get(url)
    assert response.status_code == 200, response.text
    found: list[Json] = response.json()
    return found


def triple(value: str) -> Json:
    return {"conservative": value, "base": value, "optimistic": value}


async def test_the_next_version_replaces_one_process_only(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id, doc_id, template = await analyse(env)
        before = await table(env, run_id)
        assert ids(before) == {"i1": "ini_proc_1_1", "i2": "ini_proc_1_2", "k1": "ini_proc_2_1"}
        values = {
            "ini.ini_proc_1_1.capex": "123456",
            "ini.ini_proc_1_1.proc_1.op_2.reduction_share": "0.5",
            "ini.ini_proc_1_2.capex": "777",
        }
        changes = [{"key": key, "values": triple(value)} for key, value in values.items()]
        url = f"/api/v1/analyses/{run_id}/assumptions"
        saved = await env.client.patch(url, json={"changes": changes}, headers={"If-Match": '"1"'})
        assert saved.status_code == 200, saved.text
        rejected = await review(env, run_id, entity_type="initiative", entity_id="ini_proc_1_2",
                                action="reject", comment="Не подходит")  # fmt: skip
        assert rejected.status_code == 201
        renamed = await review(env, run_id, entity_type="operation", entity_id="proc_1.op_2",
                               action="edit", patch={"name": "Сверка комплектности"})  # fmt: skip
        assert renamed.json()["stale_initiative_ids"] == ["ini_proc_1_1"]

        calls = len(env.llm.calls)
        env.llm.add(StageName.OPPORTUNITIES, opportunities(template, SECOND))
        started = await regenerate(env, run_id)
        assert started.status_code == 202, started.text
        child = started.json()
        assert (child["parent_run_id"], child["process_id"]) == (run_id, "proc_1")
        done = await env.finish(child["id"])
        assert done["status"] == "succeeded", done
        assert {s["name"]: s["status"] for s in done["stages"]} == {
            "discover": "skipped",
            "detail": "skipped",
            "validate": "succeeded",  # the process as the expert left it
            "opportunities": "succeeded",
            "postprocess": "succeeded",
        }
        (call,) = env.llm.calls[calls:]  # S6 of that process alone, from the edited model
        assert call.stage == StageName.OPPORTUNITIES and call.run_id == child["id"]
        assert "Сверка комплектности" in call.user and "proc_2.op_1" not in call.user

        after = await table(env, run_id)
        assert ids(after) == {
            "j1": "ini_proc_1_v2_1",
            "j2": "ini_proc_1_v2_2",
            "k1": "ini_proc_2_1",
        }
        other, earlier = row(after, "ini_proc_2_1"), row(before, "ini_proc_2_1")
        assert other["initiative"] == earlier["initiative"]  # not touched (its rank may move)
        assert other["economics"]["solo"] == earlier["economics"]["solo"]
        assert not any(r["initiative"]["stale"] for r in after["rows"])  # stale cleared
        assert STALE_BANNER not in after["banners"]
        items = {
            i["key"]: i
            for i in (await env.client.get(f"/api/v1/analyses/{run_id}/assumptions")).json()[
                "items"
            ]
        }
        carried = items["ini.ini_proc_1_v2_1.capex"]
        assert carried["source"] == "user" and carried["values"]["base"] == "123456"
        share = items["ini.ini_proc_1_v2_1.proc_1.op_2.reduction_share"]
        assert share["source"] == "user" and share["values"]["base"] == "0.5"
        assert items["ini.ini_proc_1_v2_2.capex"]["source"] == "default_profile"  # no match
        assert items["ini.ini_proc_1_2.capex"]["values"]["base"] == "777"  # kept in the set

        history = await versions(env, run_id)
        assert [(v["version"], v["current"], v["run_id"]) for v in history] == [
            (1, False, run_id),
            (2, True, child["id"]),
        ]
        assert [i["id"] for i in history[0]["result"]["initiatives"]] == [
            "ini_proc_1_1",
            "ini_proc_1_2",
        ]  # the old version stays readable
        assert history[1]["predecessors"] == {"ini_proc_1_v2_1": "ini_proc_1_1"}
        assert history[1]["without_successor"] == ["ini_proc_1_2"]
        assert [v["version"] for v in await versions(env, run_id, "proc_2")] == [1]

        url = f"/api/v1/analyses/{run_id}/initiatives/ini_proc_1_v2_2"
        card = (await env.client.get(url)).json()
        assert card["version"] == 2 and card["history"] == []
        previous = [(d["version"], d["title"], d["event"]["action"])
                    for d in card["previous_decisions"]]  # fmt: skip
        assert previous == [(1, "Инициатива i2", "reject")]
        old = await review(env, run_id, entity_type="initiative", entity_id="ini_proc_1_1",
                           action="confirm")  # fmt: skip
        assert old.status_code == 404  # decisions go to the effective version
        later = await review(env, run_id, entity_type="operation", entity_id="proc_1.op_2",
                             action="edit", patch={"name": "Сверка перечня"})  # fmt: skip
        assert later.json()["stale_initiative_ids"] == ["ini_proc_1_v2_1"]

        listed = (await env.client.get(f"/api/v1/documents/{doc_id}/analyses")).json()
        assert [a["id"] for a in listed] == [run_id]  # a regeneration is not an analysis
        own = await env.client.get(f"/api/v1/analyses/{child['id']}/initiatives")
        assert own.status_code == 404 and own.json()["error"]["details"]["run_id"] == run_id
        reads = await table(env, run_id)

    async for env in make_env():  # a new application on the same database
        assert await table(env, run_id) == reads


async def test_a_failed_regeneration_keeps_the_previous_version(
    make_env: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async for env in make_env():
        run_id, doc_id, template = await analyse(env)
        assert (await regenerate(env, run_id, "proc_9")).status_code == 404
        dropped = await review(env, run_id, entity_type="process", entity_id="proc_2",
                               action="reject", comment="Не процесс")  # fmt: skip
        assert dropped.status_code == 201
        refused = await regenerate(env, run_id, "proc_2")
        assert refused.status_code == 400 and "отклонены" in refused.json()["error"]["message"]

        env.llm.add(StageName.OPPORTUNITIES, LLMTimeoutError())
        failed = (await regenerate(env, run_id)).json()
        done = await env.finish(failed["id"])
        assert done["status"] == "failed" and done["error"]["code"] == "llm_timeout"
        assert set(ids(await table(env, run_id)).values()) >= {"ini_proc_1_1", "ini_proc_1_2"}
        assert [v["version"] for v in await versions(env, run_id)] == [1]
        assert (await regenerate(env, failed["id"])).status_code == 404  # not an analysis

        env.llm.add(StageName.OPPORTUNITIES, opportunities(template, SECOND))
        resumed = await env.client.post(f"/api/v1/analyses/{failed['id']}/resume")
        assert resumed.status_code == 202, resumed.text
        assert (await env.finish(failed["id"]))["status"] == "succeeded"
        assert [v["version"] for v in await versions(env, run_id)] == [1, 2]

        unknown = json.loads(await env.answer(doc_id, "opp_unknown_ids"))
        env.llm.add(StageName.OPPORTUNITIES, unknown)
        useless = (await regenerate(env, run_id)).json()
        done = await env.finish(useless["id"])
        assert done["status"] == "failed" and "прежняя версия" in done["error"]["message"]
        assert [(v["version"], v["current"]) for v in await versions(env, run_id)] == [
            (1, False),
            (2, True),
        ]

        # One run at a time per document: a queued regeneration blocks the next one.
        monkeypatch.setattr(env.state.runner, "_spawn", lambda run_id: None)
        queued = await regenerate(env, run_id)
        assert queued.status_code == 202
        busy = await regenerate(env, run_id)
        assert busy.status_code == 409
        assert busy.json()["error"]["details"]["run_id"] == queued.json()["id"]
        again = await env.client.post(f"/api/v1/documents/{doc_id}/analyses")
        assert again.status_code == 409
        monkeypatch.undo()
        env.llm.add(StageName.OPPORTUNITIES, opportunities(template, SECOND))
        await env.state.runner.recover()  # picks the queued run up
        assert (await env.finish(queued.json()["id"]))["status"] == "succeeded"
        assert [v["version"] for v in await versions(env, run_id)] == [1, 2, 3]
        assert "ini_proc_1_v3_1" in ids(await table(env, run_id)).values()


async def test_regeneration_from_the_process_map(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id, _, template = await analyse(env)
        rejected = await review(env, run_id, entity_type="initiative", entity_id="ini_proc_1_2",
                                action="reject", comment="Не подходит")  # fmt: skip
        assert rejected.status_code == 201
        await review(env, run_id, entity_type="operation", entity_id="proc_1.op_2",
                     action="edit", patch={"name": "Сверка комплектности"})  # fmt: skip
        page = (await env.client.get(f"/analyses/{run_id}/processes?process=proc_1")).text
        assert "Инициативы процесса" in page and "требуют пересмотра: 1" in page
        action = f"/analyses/{run_id}/processes/proc_1/regenerate"
        assert f'action="{action}"' in page

        env.llm.add(StageName.OPPORTUNITIES, opportunities(template, SECOND))
        started = await env.client.post(action)
        assert started.status_code == 303
        status_url = started.headers["location"]
        child_id = status_url.rsplit("/", 1)[-1]
        await env.state.runner.wait(child_id)
        status = (await env.client.get(status_url)).text
        assert "Перегенерация инициатив процесса" in status
        assert "«Выдача пропуска»" in status and "S5 · процесс с решениями эксперта" in status
        assert f"/analyses/{run_id}/initiatives?process_id=proc_1" in status

        page = (await env.client.get(f"/analyses/{run_id}/processes?process=proc_1")).text
        assert "Версия 2" in page and "требуют пересмотра" not in page
        card = (await env.client.get(f"/analyses/{run_id}/initiatives/ini_proc_1_v2_2")).text
        assert "Решения по предыдущей версии" in card and "«Инициатива i2»" in card
        refused = await env.client.post(f"/analyses/{run_id}/processes/proc_9/regenerate")
        assert refused.status_code == 404 and "Процесс не найден" in refused.text
        log = (await env.client.get(f"/analyses/{run_id}/history")).text
        assert "Инициатива i2" in log and "предыдущая версия 1" in log  # the replaced one
