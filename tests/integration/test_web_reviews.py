"""Expert review on screens 3 and 5, and the history screen 7 (ui.md, FR-080…FR-082, T053).

Forms are posted the way a browser posts them: every field of the form as the page rendered
it, some of them changed.
"""

from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

import httpx
from test_analysis_e2e import Env
from test_analysis_e2e import make_env as make_env
from test_reviews_api import analyse, history, ids, table

from gr.analysis.overlay import EDITABLE
from gr.web.reviews import FORM_FIELDS

Form = dict[str, str]


class _Forms(HTMLParser):
    """Every form of a page with the values a browser would send."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self._select: str | None = None
        self._textarea: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        name = found.get("name") or ""
        if tag == "form":
            self.forms.append({})
        elif not self.forms:
            return
        elif tag == "input" and name:
            self.forms[-1][name] = found.get("value") or ""
        elif tag == "select":
            self._select = name
        elif tag == "option" and self._select:
            value = found.get("value") or ""
            self.forms[-1].setdefault(self._select, value)  # the first one, unless one is selected
            if "selected" in found:
                self.forms[-1][self._select] = value
        elif tag == "textarea" and name:
            self._textarea = name
            self.forms[-1][name] = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data: str) -> None:
        if self._textarea and self.forms:
            self.forms[-1][self._textarea] += data


def form(html: str, entity_id: str, action: str) -> Form:
    parser = _Forms()
    parser.feed(html)
    found = [
        f for f in parser.forms if (f.get("entity_id"), f.get("action")) == (entity_id, action)
    ]
    assert len(found) == 1, (entity_id, action, len(found))
    return found[0]


async def post(env: Env, run_id: str, data: Form) -> httpx.Response:
    return await env.client.post(f"/analyses/{run_id}/reviews", data=data)


async def screen(env: Env, url: str) -> str:
    response = await env.client.get(url)
    assert response.status_code == 200, response.text
    return response.text


async def test_a_rejection_without_a_comment_is_refused(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        url = f"/analyses/{run_id}/processes?process=proc_1"
        page = await screen(env, url)
        assert 'name="comment" rows="2" maxlength="2000" required' in page  # the browser asks
        reject = form(page, "proc_1.op_1", "reject")
        logged = len(await history(env, run_id))

        refused = await post(env, run_id, reject | {"comment": "   "})  # a crafted request
        assert refused.status_code == 400
        assert "Отклонение требует комментария" in refused.text
        assert f'href="{url}#proc_1.op_1"' in refused.text  # back to the form
        assert len(await history(env, run_id)) == logged

        done = await post(env, run_id, reject | {"comment": "Не процедура регламента"})
        assert done.status_code == 303 and done.headers["location"] == f"{url}#proc_1.op_1"
        page = await screen(env, url)
        assert '<tr id="proc_1.op_1" class="rejected">' in page
        assert "Отклонено" in page
        assert form(page, "proc_1.op_1", "reopen")  # the only decision left: reopen
        assert "Все операции инициативы отклонены экспертом." in await screen(
            env, f"/analyses/{run_id}/initiatives"
        )


async def test_decisions_on_the_process_map(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        url = f"/analyses/{run_id}/processes?process=proc_1"
        page = await screen(env, url)
        assert (await post(env, run_id, form(page, "proc_1.op_2", "confirm"))).status_code == 303

        page = await screen(env, url)
        edit = form(page, "proc_1.op_1", "edit")
        assert edit["f:name"] == "Регистрация заявления" and edit["f:deadline.unit"] == ""
        same = await post(env, run_id, edit)
        assert same.status_code == 400 and "Изменений нет" in same.text
        changed = edit | {
            "f:name": "Приём и регистрация", "f:deadline.value": "1",
            "f:deadline.unit": "working_days", "f:deadline.raw": "один рабочий день",
        }  # fmt: skip
        assert (await post(env, run_id, changed)).status_code == 303
        (event,) = [e for e in await history(env, run_id) if e["entity_id"] == "proc_1.op_1"]
        assert set(event["patch"]) == {"name", "deadline"}  # the untouched fields are not sent
        page = await screen(env, url)
        assert "Приём и регистрация" in page and "один рабочий день" in page
        assert page.count('title="Исправлено экспертом') >= 2

        quoted = form(page, "proc_1.op_2", "edit") | {
            "q:action": "проверяет комплектность документов, указанных в пункте 2.2"
        }
        assert (await post(env, run_id, quoted)).status_code == 303
        processes = (await env.client.get(f"/api/v1/analyses/{run_id}/processes")).json()
        (op,) = [o for o in processes["processes"][0]["operations"] if o["id"] == "proc_1.op_2"]
        assert op["action"]["layer"] == "fact" and op["review"]["status"] == "edited"
        invented = form(await screen(env, url), "proc_1.op_2", "edit") | {
            "q:performer": "проверку выполняет робот без участия людей"
        }
        refused = await post(env, run_id, invented)
        assert refused.status_code == 400 and "Цитата не подтверждена" in refused.text

        flagged = form(await screen(env, url), "proc_1.op_1", "edit")
        assert flagged["f:legally_significant"] == "no"
        signed = flagged | {"f:legally_significant": "yes"}
        assert (await post(env, run_id, signed)).status_code == 303
        cleared = form(await screen(env, url), "proc_1.op_1", "edit") | {
            "f:legally_significant": "no"
        }
        refused = await post(env, run_id, cleared)
        assert refused.status_code == 400 and "только с комментарием" in refused.text
        explained = cleared | {"comment": "Регистрация не влечёт юридических последствий"}
        assert (await post(env, run_id, explained)).status_code == 303

        reopen = form(await screen(env, url), "proc_1.op_2", "reopen")
        assert (await post(env, run_id, reopen)).status_code == 303
        assert form(await screen(env, url), "proc_1.op_2", "confirm")  # pending again

        process = form(await screen(env, url), "proc_1", "edit") | {"f:goal": "выдать пропуск"}
        assert (await post(env, run_id, process)).status_code == 303
        assert "выдать пропуск" in await screen(env, url)

        elsewhere = await post(env, run_id, reopen | {"back": "https://example.com/"})
        assert elsewhere.status_code == 400  # pending: nothing to reopen
        assert 'href="https://example.com/"' not in elsewhere.text
        confirm = form(await screen(env, url), "proc_1.op_2", "confirm")
        moved = await post(env, run_id, confirm | {"back": "https://example.com/"})
        assert moved.headers["location"] == f"/analyses/{run_id}/processes"  # never another site


async def test_decisions_on_the_initiative_card(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        key = ids(await table(env, run_id))
        url = f"/analyses/{run_id}/initiatives/{key['i1']}"
        card = await screen(env, url)
        edit = form(card, key["i1"], "edit")
        assert edit["f:ratings.feasibility.level"] == "medium"
        assert edit["f:automation_level"] == "assistive"
        changed = edit | {"f:title": "Сверка по перечню", "f:ratings.feasibility.level": "high",
                          "f:risks": "Перечень меняется\nНет реестра"}  # fmt: skip
        assert (await post(env, run_id, changed)).status_code == 303
        (event,) = [e for e in await history(env, run_id) if e["entity_id"] == key["i1"]]
        assert set(event["patch"]) == {"title", "ratings", "risks"}
        assert set(event["patch"]["ratings"]) == {"feasibility"}  # only the criterion rated again
        assert event["patch"]["risks"] == ["Перечень меняется", "Нет реестра"]
        card = await screen(env, url)
        assert "Сверка по перечню" in card and "Нет реестра" in card

        block = form(card, key["i1"], "set_gate")
        assert block["f:status"] == "blocked"
        refused = await post(env, run_id, block)
        assert refused.status_code == 400 and "требует комментария" in refused.text
        blocked = await post(env, run_id, block | {"comment": "Нет правового основания"})
        assert blocked.status_code == 303
        card = await screen(env, url)
        assert "Эксперт запретил инициативу." in card
        unblock = form(card, key["i1"], "set_gate")
        assert unblock["f:status"] == "allowed"
        assert (await post(env, run_id, unblock)).status_code == 303
        assert "Эксперт запретил инициативу." not in await screen(env, url)

        finding = await env.client.get(f"/api/v1/analyses/{run_id}/initiatives/{key['i1']}")
        (found,) = finding.json()["findings"]
        edit_finding = form(await screen(env, url), found["id"], "edit") | {
            "f:pattern": "lookup_and_reconciliation"
        }
        assert (await post(env, run_id, edit_finding)).status_code == 303
        assert "Поиск и сверка" in await screen(env, url)

        pair = "|".join(sorted(key.values()))
        related = await post(env, run_id, {"entity_type": "relation", "entity_id": pair,
                                           "action": "edit", "f:relation": "alternative",
                                           "back": url})  # fmt: skip
        assert related.status_code == 303
        card = await screen(env, url)
        assert "задано экспертом" in card
        assert form(card, pair, "edit")["f:relation"] == "alternative"

        reject = form(card, key["i1"], "reject") | {"comment": "Дублирует i2"}
        assert (await post(env, run_id, reject)).status_code == 303
        card = await screen(env, url)
        assert "Отклонена экспертом. Эффект не рассчитывается." in card
        assert form(card, key["i1"], "reopen")


async def test_the_history_shows_what_each_decision_changed(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)  # its volumes are an assumption change by the user
        key = ids(await table(env, run_id))
        url = f"/analyses/{run_id}/processes?process=proc_1"
        edit = form(await screen(env, url), "proc_1.op_1", "edit") | {
            "f:deadline.value": "2", "f:deadline.unit": "working_days",
            "f:deadline.raw": "два рабочих дня", "comment": "Срок из п. 3.2.1",
        }  # fmt: skip
        assert (await post(env, run_id, edit)).status_code == 303
        card = await screen(env, f"/analyses/{run_id}/initiatives/{key['i1']}")
        reject = form(card, key["i1"], "reject") | {"comment": "Перечень меняется"}
        assert (await post(env, run_id, reject)).status_code == 303
        weights = (await env.client.get(f"/api/v1/analyses/{run_id}/priority-config")).json()
        weights["cutoff_score"] = "20"
        saved = await env.client.put(f"/api/v1/analyses/{run_id}/priority-config",
                                     json=weights, headers={"If-Match": '"1"'})  # fmt: skip
        assert saved.status_code == 200

        page = await screen(env, f"/analyses/{run_id}/history")
        rows = page.split("<tbody>", 1)[1].split("</tbody>", 1)[0].split("<tr>")[1:]
        assert len(rows) == 4  # the latest first
        assert "Веса и пороги" in rows[0] and "Порог отсечения" in rows[0] and "35 → 20" in rows[0]
        assert "Отклонено" in rows[1] and "Перечень меняется" in rows[1]
        assert "Инициатива" in rows[1] and f'href="/analyses/{run_id}/initiatives/' in rows[1]
        assert "<strong>Срок</strong>: — → два рабочих дня" in rows[2]
        assert "1 · Регистрация заявления" in rows[2] and "Срок из п. 3.2.1" in rows[2]
        assert "Объём процесса («Выдача пропуска»)" in rows[3]
        assert "700 / 1000 / 1300 → 100000 / 100000 / 100000" in rows[3].replace(" ", "")

        only = await screen(env, f"/analyses/{run_id}/history?entity_type=operation")
        assert "два рабочих дня" in only and "Перечень меняется" not in only
        one = await screen(env, f"/analyses/{run_id}/history?entity_id={key['i1']}")
        assert "Перечень меняется" in one and "два рабочих дня" not in one
        assert "вся история" in one
        nobody = await screen(env, f"/analyses/{run_id}/history?user=nobody")
        assert "Решений пока нет." in nobody
        assert (await env.client.get("/analyses/run_unknown/history")).status_code == 404


def test_the_forms_offer_exactly_what_an_expert_may_correct() -> None:
    assert {kind: set(fields) for kind, fields in FORM_FIELDS.items()} == {
        kind: set(fields) for kind, fields in EDITABLE.items() if kind != "relation"
    }
