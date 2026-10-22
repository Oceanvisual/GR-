"""Screen 6 (model settings) and the economics on screens 4–5 (ui.md, FR-058, T046).

The form is posted the way a browser does: every input of the page, some of them changed.
"""

from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

import httpx
from test_analysis_e2e import Env
from test_analysis_e2e import make_env as make_env
from test_assumptions_api import analyse

from gr.services.results import ASSUMPTIONS_ENTITY, MODEL_DISCLAIMER

FR_058 = "Денежный эквивалент высвобождаемого рабочего времени (модельная оценка)"
STUBS = "Объёмы процессов не заданы: используются заглушки"
HTMX = {"HX-Request": "true"}


class _Inputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("name"):
            self.fields[values["name"] or ""] = values.get("value") or ""


def inputs(html: str) -> dict[str, str]:
    parser = _Inputs()
    parser.feed(html)
    return parser.fields


async def page(env: Env, run_id: str) -> str:
    response = await env.client.get(f"/analyses/{run_id}/settings")
    assert response.status_code == 200, response.text
    return response.text


async def save(env: Env, run_id: str, form: dict[str, str]) -> httpx.Response:
    """Post the form the way HTMX does."""
    return await env.client.post(f"/analyses/{run_id}/settings", data=form, headers=HTMX)


async def volume_key(env: Env, run_id: str) -> str:
    table = (await env.client.get(f"/api/v1/analyses/{run_id}/initiatives")).json()
    return f"proc.{table['rows'][0]['initiative']['process_id']}.volume_per_year"


async def test_the_page_shows_every_assumption_its_source_and_the_banners(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        html = await page(env, run_id)
        assert MODEL_DISCLAIMER in html and STUBS in html and FR_058 in html
        assert "Модельное допущение (не подтверждено)" in html  # FR-052
        fields = inputs(html)
        assert fields["version"] == "1"
        assert fields["base:hourly_cost"] == "1200"
        assert fields["base:overhead_ratio"] == "0,3"  # decimal comma
        assert fields[f"base:{await volume_key(env, run_id)}"] == "1000"
        assert any(name.endswith(".reduction_share") for name in fields)
        assert "требует ввода" in html and "Итог расчёта" in html
        # How the profile got a number is shown apart; the comment field is the user's.
        assert "Профиль: время по типу операции." in html
        assert fields["note:hourly_cost"] == ""
        assert "1 · " in html and "0 · " not in html  # operations numbered as on the map


async def test_saving_with_htmx_recalculates_in_place_without_llm(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        before = inputs(await page(env, run_id))
        calls = len(env.llm.calls)
        volume = await volume_key(env, run_id)
        form = before | {
            f"conservative:{volume}": "1 500",
            f"base:{volume}": "2000",
            f"optimistic:{volume}": "2500,5",
            f"note:{volume}": "Журнал регистрации за 2025 год",
            "comment": "Объёмы по журналу",
        }
        response = await save(env, run_id, form)
        assert response.status_code == 200, response.text
        assert response.text.startswith('<div id="settings">')  # the fragment, swapped in place
        assert "Сохранено: версия 2" in response.text
        after = inputs(response.text)
        assert after["version"] == "2"
        assert (after[f"conservative:{volume}"], after[f"optimistic:{volume}"]) == (
            "1500",
            "2500,5",
        )
        assert STUBS not in response.text and "Введено пользователем" in response.text
        assert len(env.llm.calls) == calls
        assert await env.state.repos.assumptions.versions(run_id) == [1, 2]
        (event,) = await env.state.repos.reviews.list_for_run(run_id, ASSUMPTIONS_ENTITY)
        assert event.comment == "Объёмы по журналу" and list(event.patch or {}) == [volume]


async def test_typed_values_and_errors_stay_on_the_form(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        volume = await volume_key(env, run_id)
        form = inputs(await page(env, run_id)) | {
            "base:overhead_ratio": "1,5",
            "base:hourly_cost": "дорого",
            f"optimistic:{volume}": "",
        }
        response = await save(env, run_id, form)
        assert response.status_code == 200  # HTMX swaps only 2xx answers
        html = response.text
        assert "Часть значений не принята" in html
        assert "не число" in html and "заполните три значения" in html
        assert inputs(html)["base:hourly_cost"] == "дорого"  # what the user typed is kept
        assert await env.state.repos.assumptions.versions(run_id) == [1]

        # The service rule comes back next to its row too.
        form = inputs(await page(env, run_id)) | {"base:overhead_ratio": "1,5"}
        html = (await save(env, run_id, form)).text
        assert "доля должна быть от 0 до 1" in html and inputs(html)["base:overhead_ratio"] == "1,5"


async def test_without_htmx_a_save_redirects_and_errors_answer_400(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        form = inputs(await page(env, run_id)) | {"base:hourly_cost": "1300"}
        saved = await env.client.post(f"/analyses/{run_id}/settings", data=form)
        assert saved.status_code == 303
        assert saved.headers["location"] == f"/analyses/{run_id}/settings?saved=1"
        assert "Сохранено" in (await env.client.get(saved.headers["location"])).text

        bad = inputs(await page(env, run_id)) | {"base:fte_hours_per_year": "-1"}
        refused = await env.client.post(f"/analyses/{run_id}/settings", data=bad)
        assert refused.status_code == 400
        assert "<html" in refused.text and "значения не могут быть отрицательными" in refused.text


async def test_a_stale_form_reports_the_conflict_and_keeps_the_input(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        old = inputs(await page(env, run_id))
        first = old | {"base:hourly_cost": "1300"}
        await save(env, run_id, first)
        second = old | {"base:overhead_ratio": "0,4"}  # the other tab, still on version 1
        html = (await save(env, run_id, second)).text
        assert "Данные изменились в другой вкладке" in html
        assert inputs(html)["base:overhead_ratio"] == "0,4"
        assert await env.state.repos.assumptions.versions(run_id) == [1, 2]


async def test_an_unchanged_form_makes_no_version(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        form = inputs(await page(env, run_id))
        html = (await save(env, run_id, form)).text
        assert "Изменений нет." in html
        assert await env.state.repos.assumptions.versions(run_id) == [1]


async def test_the_rate_of_a_process_is_optional(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        rate = (await volume_key(env, run_id)).replace("volume_per_year", "hourly_cost_override")
        form = inputs(await page(env, run_id))
        assert form[f"base:{rate}"] == ""  # not set: the global rate applies
        noted = form | {f"note:{rate}": "ставка отдела"}
        html = (await save(env, run_id, noted)).text
        assert "сначала задайте значения" in html
        priced = form | {f"{s}:{rate}": "2400" for s in ("conservative", "base", "optimistic")}
        html = (await save(env, run_id, priced)).text
        assert "Сохранено: версия 2" in html and inputs(html)[f"base:{rate}"] == "2400"


async def test_screens_4_and_5_show_the_economics_with_its_wording(
    make_env: Callable[..., Any],
) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        table = await env.client.get(f"/analyses/{run_id}/initiatives")
        assert table.status_code == 200
        assert MODEL_DISCLAIMER in table.text and FR_058 in table.text and STUBS in table.text
        assert "₽" in table.text and "ч в 1-й год" in table.text
        assert 'value="conservative"' in table.text  # the scenario switch
        assert "Будет рассчитано" in table.text  # the priority, until US5
        conservative = await env.client.get(f"/analyses/{run_id}/initiatives?scenario=conservative")
        assert "сценарий «Консервативный»" in conservative.text

        rows = (await env.client.get(f"/api/v1/analyses/{run_id}/initiatives")).json()["rows"]
        card = await env.client.get(f"/analyses/{run_id}/initiatives/{rows[0]['initiative']['id']}")
        assert card.status_code == 200
        for text in (
            FR_058,
            "Экономический эффект",
            "Использованные допущения",
            "ROI за горизонт",
            "Консервативный",
            "Оптимистичный",
            "Стоимость часа",
            "Изменить допущения",
        ):
            assert text in card.text, text  # fmt: skip
        assert "Будет рассчитано вместе с рейтингом" in card.text


async def test_scenarios_out_of_order_are_saved_and_marked(make_env: Callable[..., Any]) -> None:
    async for env in make_env():
        run_id = await analyse(env)
        form = inputs(await page(env, run_id)) | {"base:hourly_cost": "1300"}  # 1200/1300/1200
        html = (await save(env, run_id, form)).text
        assert "Сохранено: версия 2" in html  # FR-057: a warning, not a refusal
        assert "сценарии не по порядку" in html and "от консервативного к оптимистичному" in html
