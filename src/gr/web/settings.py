"""Screen 6, model settings: the assumptions (ui.md, FR-050…FR-057, T046; weights come in T049).

The whole set is one form: three scenario values and a comment per row. On save only the rows
that differ from the version the form was built from go to the service, as one change of the
set: a new version, refused if another change came first. With HTMX the form comes back in
place — errors next to their rows with the typed values kept, or the new version with the
recalculated effect; without it the browser gets the page again (or a redirect on success).
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from gr.analysis.defaults import assumption_unit
from gr.analysis.economics import out_of_order
from gr.api.deps import AppState, StateDep, UserDep, require_user
from gr.domain.assumptions import (
    Assumption,
    AssumptionSet,
    global_key,
    initiative_key,
    initiative_operation_key,
    operation_key,
    process_key,
)
from gr.domain.enums import AssumptionSource
from gr.domain.initiative import Initiative
from gr.domain.priority import PriorityConfig
from gr.domain.process import ProcessModel
from gr.errors import AppError
from gr.services.results import (
    MODEL_DISCLAIMER,
    AssumptionChange,
    AssumptionsPatch,
    InitiativeTable,
    ResultsService,
    TripleIn,
)
from gr.web.format import plain
from gr.web.labels import LABELS
from gr.web.routes import templates

router = APIRouter(dependencies=[Depends(require_user)], include_in_schema=False)

SCENARIOS = ("conservative", "base", "optimistic")
MAX_FIELDS = 50_000  # four inputs a row; a large regulation has a few thousand rows
MAX_NOTE = 2000
GLOBALS = ("hourly_cost", "overhead_ratio", "fte_hours_per_year", "horizon_years")
INITIATIVE = ("capex", "fixed_opex_per_year", "adoption_y1", "adoption_y2", "adoption_y3")
PAIR = ("reduction_share", "review_hours", "variable_cost_per_exec")


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    unit: str
    values: tuple[str, str, str]  # as shown in the inputs
    note: str  # the user's comment
    source: str | None  # None: an optional key not set (the rate of a process)
    requires_input: bool = False
    derivation: str = ""  # how the profile got a default value; not a comment to edit
    out_of_order: bool = False  # scenarios not from the conservative to the optimistic
    error: str | None = None


@dataclass(frozen=True)
class Group:
    title: str
    rows: list[Row]


@dataclass(frozen=True)
class Section:
    title: str
    groups: list[Group]


def _row(key: str, label: str, items: Mapping[str, Assumption], unit: str = "") -> Row:
    item = items.get(key)
    if item is None:
        return Row(key, label, unit, ("", "", ""), "", None)
    shown = tuple(plain(value) for value in item.values.as_tuple)
    default = item.source == AssumptionSource.DEFAULT_PROFILE
    return Row(
        key,
        label,
        item.unit,
        (shown[0], shown[1], shown[2]),
        "" if default else item.note or "",
        item.source.value,
        item.requires_input,
        derivation=(item.note or "") if default else "",
        out_of_order=out_of_order(item),
    )


def sections(
    model: ProcessModel, initiatives: list[Initiative], assumptions: AssumptionSet
) -> list[Section]:
    items = assumptions.by_key()
    names: dict[str, str] = {}
    label: dict[str, str] = LABELS["assumption"]
    general = Group("", [_row(global_key(k), label[k], items) for k in GLOBALS])
    processes: list[Group] = []
    for process in model.processes:
        rows = [
            _row(process_key(process.id, "volume_per_year"), label["volume_per_year"], items),
            _row(
                process_key(process.id, "hourly_cost_override"),
                f"{label['hourly_cost_override']} (если отличается от общей)",
                items,
                assumption_unit("hourly_cost_override"),
            ),
        ]
        for op in process.operations:
            names[op.id] = f"{op.seq + 1} · {op.name}"  # numbered as on the process map
            rows += [
                _row(operation_key(op.id, k), f"{names[op.id]}: {label[k].lower()}", items)
                for k in ("time_hours", "case_share")
            ]
        processes.append(Group(f"Процесс «{process.name.value}»", rows))
    proposals: list[Group] = []
    for initiative in initiatives:
        rows = [_row(initiative_key(initiative.id, k), label[k], items) for k in INITIATIVE]
        for op_id in initiative.operation_ids:
            rows += [
                _row(
                    initiative_operation_key(initiative.id, op_id, k),
                    f"{names.get(op_id, op_id)}: {label[k].lower()}",
                    items,
                )
                for k in PAIR
            ]
        proposals.append(Group(f"Инициатива «{initiative.title}»", rows))
    return [
        Section("Общие параметры", [general]),
        Section("Процессы и операции", processes),
        Section("Инициативы", proposals),
    ]


def _number(text: str) -> Decimal:
    value = Decimal("".join(text.split()).replace(",", ".").replace("−", "-"))
    if not value.is_finite():
        raise InvalidOperation(text)
    return value


def _values(texts: tuple[str, str, str]) -> tuple[Decimal, Decimal, Decimal]:
    first, second, third = (_number(text) for text in texts)
    return first, second, third


def read_form(
    form: Mapping[str, str], rows: list[Row]
) -> tuple[list[AssumptionChange], dict[str, str], dict[str, Row]]:
    """Changes against the shown values, errors by key, and the rows as typed (to show again)."""
    changes: list[AssumptionChange] = []
    errors: dict[str, str] = {}
    typed: dict[str, Row] = {}
    for row in rows:
        fields = [f"{scenario}:{row.key}" for scenario in SCENARIOS]
        if not any(name in form for name in [*fields, f"note:{row.key}"]):
            continue  # not on the submitted form
        texts = (form.get(fields[0], "").strip(), form.get(fields[1], "").strip(),
                 form.get(fields[2], "").strip())  # fmt: skip
        note = form.get(f"note:{row.key}", row.note).strip()
        typed[row.key] = replace(row, values=texts, note=note)
        if row.source is None and not any(texts):
            if note:
                errors[row.key] = "сначала задайте значения"
            continue  # an optional rate left unset
        if not all(texts):
            errors[row.key] = "заполните три значения"
            continue
        try:
            numbers = _values(texts)
        except InvalidOperation:
            errors[row.key] = "не число"
            continue
        same = row.source is not None and numbers == _values(row.values)
        values = None if same else TripleIn(
            conservative=numbers[0], base=numbers[1], optimistic=numbers[2]
        )  # fmt: skip
        if len(note) > MAX_NOTE:
            errors[row.key] = f"комментарий длиннее {MAX_NOTE} символов"
            continue
        changed_note = note if note != row.note else None
        if values is not None or changed_note is not None:
            changes.append(AssumptionChange(key=row.key, values=values, note=changed_note))
    return changes, errors, typed


async def _context(
    state: AppState,
    run_id: str,
    *,
    typed: Mapping[str, Row] | None = None,
    errors: Mapping[str, str] | None = None,
    message: str | None = None,
    problem: str | None = None,
) -> dict[str, Any]:
    service = ResultsService(state)
    effective, current = await service.inputs(run_id)
    table = await service.table(run_id)
    shown = [
        replace(
            section,
            groups=[
                replace(
                    group,
                    rows=[
                        replace((typed or {}).get(row.key, row), error=(errors or {}).get(row.key))
                        for row in group.rows
                    ],
                )
                for group in section.groups
            ],
        )
        for section in sections(effective.calculation_model(), effective.calculable(), current)
    ]
    return {
        "run": await service.analyses.get(run_id),
        "sections": shown,
        "version": current.version,
        "table": table,
        "disclaimer": MODEL_DISCLAIMER,
        "scenarios": SCENARIOS,
        "errors": errors or {},
        "message": message,
        "problem": problem,
    }


# --- weights and thresholds of the Priority Score (T049) -------------------------------------

SCALARS: dict[str, tuple[str, str]] = {  # field → (label, what the model accepts)
    "effect_ref": ("Опорный эффект, ₽: накопленный за горизонт эффект с оценкой 1", "больше 0"),
    "tech_guard_penalty": ("Штраф реализуемости, если технология требует обоснования", "от 0 до 1"),
    "cutoff_score": ("Порог отсечения балла (ниже — Not Recommended)", "от 0 до 100"),
    "ease_threshold": ("Порог лёгкости внедрения для Quick Wins", "от 0 до 1"),
    "effect_threshold": ("Порог нормированного эффекта для Strategic Projects", "от 0 до 1"),
}
MAPS: dict[str, tuple[str, str, str]] = {  # model field → (form prefix, title, error)
    "weights": ("w", "Веса критериев", "Веса — неотрицательные числа с суммой больше 0."),
    "level_map": ("lv", "Качественные оценки → число", "Значения — от 0 до 1."),
    "duration_map": ("du", "Срок внедрения → число", "Значения — от 0 до 1."),
}
MAP_LABELS = {"weights": "criterion", "level_map": "level", "duration_map": "duration"}


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    value: str
    error: str | None = None


@dataclass(frozen=True)
class FieldGroup:
    key: str
    title: str
    fields: list[Field]
    error: str | None = None


def priority_groups(
    config: PriorityConfig,
    typed: Mapping[str, str] | None = None,
    errors: Mapping[str, str] | None = None,
) -> list[FieldGroup]:
    typed, errors = typed or {}, errors or {}

    def field(name: str, label: str, value: Decimal) -> Field:
        return Field(name, label, typed.get(name, plain(value)), errors.get(name))

    groups = []
    for key, (prefix, title, _) in MAPS.items():
        values: Mapping[str, Decimal] = getattr(config, key)
        names: dict[str, str] = LABELS[MAP_LABELS[key]]
        fields = [field(f"{prefix}:{k}", names[k], v) for k, v in values.items()]
        groups.append(FieldGroup(key, title, fields, errors.get(key)))
    scalars = [field(k, label, getattr(config, k)) for k, (label, _) in SCALARS.items()]
    groups.insert(1, FieldGroup("thresholds", "Пороги и опорные величины", scalars))
    return groups


def read_priority_form(
    form: Mapping[str, str], current: PriorityConfig
) -> tuple[PriorityConfig | None, dict[str, str], dict[str, str]]:
    """The configuration typed, errors by field or group, and the typed text (to show again)."""
    errors: dict[str, str] = {}
    typed: dict[str, str] = {}
    data: dict[str, Any] = {}

    def number(name: str) -> Decimal | None:
        typed[name] = form.get(name, "").strip()
        try:
            return _number(typed[name])
        except InvalidOperation:
            errors[name] = "не число"
            return None

    for key, (prefix, _, _) in MAPS.items():
        data[key] = {k: number(f"{prefix}:{k}") for k in getattr(current, key)}
    for name in SCALARS:
        data[name] = number(name)
    if errors:
        return None, errors, typed
    try:
        return PriorityConfig.model_validate(data), errors, typed
    except ValidationError as exc:
        for error in exc.errors():
            loc = error.get("loc", ())
            message = str(error.get("msg", ""))
            target = next((key for key in MAPS if key in message or key in loc), None)
            if target is not None:
                errors[target] = MAPS[target][2]
            elif loc and str(loc[0]) in SCALARS:
                errors[str(loc[0])] = SCALARS[str(loc[0])][1].capitalize() + "."
        if not errors:
            errors["thresholds"] = "Некорректная конфигурация."
        return None, errors, typed


async def _priority_context(
    state: AppState,
    run_id: str,
    *,
    table: InitiativeTable | None = None,
    typed: Mapping[str, str] | None = None,
    errors: Mapping[str, str] | None = None,
    message: str | None = None,
    problem: str | None = None,
) -> dict[str, Any]:
    service = ResultsService(state)
    current = await service.priority_config(run_id)
    table = table or await service.table(run_id)
    return {
        "run": await service.analyses.get(run_id),
        "priority_version": current.version,
        "priority_groups": priority_groups(current.config, typed, errors),
        "ranking": table.rows,
        "priority_message": message,
        "priority_problem": problem,
    }


def _error_page(request: Request, error: AppError) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"error": error}, status_code=error.status
    )


@router.get("/analyses/{run_id}/settings", response_class=HTMLResponse)
async def settings_page(request: Request, run_id: str, state: StateDep) -> Response:
    try:
        context = await _context(state, run_id)
        context |= await _priority_context(state, run_id, table=context["table"])
    except AppError as error:
        return _error_page(request, error)
    saved = request.query_params.get("saved")
    if saved == "priority":
        context["priority_message"] = "Сохранено. Рейтинг пересчитан без обращения к LLM."
    elif saved:
        context["message"] = "Сохранено. Экономика пересчитана без обращения к LLM."
    return templates.TemplateResponse(request, "settings.html", context)


@router.post("/analyses/{run_id}/settings", response_class=HTMLResponse)
async def save_settings(request: Request, run_id: str, state: StateDep, user: UserDep) -> Response:
    htmx = request.headers.get("HX-Request") == "true"
    service = ResultsService(state)
    try:
        effective, current = await service.inputs(run_id)
    except AppError as error:
        return _error_page(request, error)
    form = await request.form(max_files=0, max_fields=MAX_FIELDS)
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    rows = [
        row
        for section in sections(effective.calculation_model(), effective.calculable(), current)
        for group in section.groups
        for row in group.rows
    ]
    changes, errors, typed = read_form(values, rows)
    message: str | None = None
    problem: str | None = None
    version = values.get("version", "")
    if not version.isdigit():
        problem = "Форма устарела: обновите страницу."
    elif not errors and not changes:
        message = "Изменений нет."
    elif not errors:
        comment = values.get("comment", "").strip() or None
        try:
            update = await service.update_assumptions(
                run_id,
                AssumptionsPatch(changes=changes, comment=comment),
                version=int(version),
                user=user,
            )
        except ValidationError:  # the comment or the number of changes over the limit
            problem = f"Комментарий длиннее {MAX_NOTE} символов или слишком много изменений сразу."
        except AppError as error:
            keys = error.details.get("keys") if error.code == "validation_error" else None
            if isinstance(keys, dict):
                errors = {str(k): str(v) for k, v in keys.items()}
            else:
                problem = error.message_ru
        else:
            if not htmx:
                return RedirectResponse(f"/analyses/{run_id}/settings?saved=1", status_code=303)
            message = (
                f"Сохранено: версия {update.assumption_set.version}. "
                "Экономика пересчитана без обращения к LLM."
            )
            typed = {}
    if errors:
        problem = problem or "Часть значений не принята: исправьте отмеченные строки."
    context = await _context(
        state, run_id, typed=typed, errors=errors, message=message, problem=problem
    )
    if not htmx:
        context |= await _priority_context(state, run_id, table=context["table"])
    failed = problem is not None
    return templates.TemplateResponse(
        request,
        "partials/settings_form.html" if htmx else "settings.html",
        context,
        status_code=400 if failed and not htmx else 200,  # HTMX swaps only 2xx answers
    )


@router.post("/analyses/{run_id}/settings/priority", response_class=HTMLResponse)
async def save_priority(request: Request, run_id: str, state: StateDep, user: UserDep) -> Response:
    htmx = request.headers.get("HX-Request") == "true"
    service = ResultsService(state)
    try:
        current = await service.priority_config(run_id)
    except AppError as error:
        return _error_page(request, error)
    form = await request.form(max_files=0, max_fields=100)
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    config, errors, typed = read_priority_form(values, current.config)
    message: str | None = None
    problem: str | None = None
    version = values.get("priority_version", "")
    if not version.isdigit():
        problem = "Форма устарела: обновите страницу."
    elif config is None:
        problem = "Часть значений не принята: исправьте отмеченные поля."
    else:
        try:
            update = await service.update_priority_config(
                run_id, config, version=int(version), user=user
            )
        except AppError as error:
            problem = error.message_ru
        else:
            if not htmx:
                return RedirectResponse(
                    f"/analyses/{run_id}/settings?saved=priority#priority", status_code=303
                )
            message = (
                "Изменений нет."
                if update.version == int(version)
                else f"Сохранено: версия {update.version}. Рейтинг пересчитан без обращения к LLM."
            )
            typed = {}
    context = await _priority_context(
        state, run_id, typed=typed, errors=errors, message=message, problem=problem
    )
    if not htmx:
        context = await _context(state, run_id) | context
    return templates.TemplateResponse(
        request,
        "partials/priority_form.html" if htmx else "settings.html",
        context,
        status_code=400 if problem and not htmx else 200,
    )
