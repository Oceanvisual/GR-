"""Expert review on the screens (ui.md 3, 5, 7; FR-080…FR-082, T053).

A decision is a plain form posted to /analyses/{run}/reviews and checked by the same service as
the API; the browser then goes back to where the form was (303) or gets the reason of a refusal
(400). An edit sends only the fields whose value the expert changed: an unchanged field keeps
its basis and is not marked as corrected. Screen 7 is the log of the run, the latest decision
first, with what each decision changed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from gr.analysis.overlay import Element
from gr.analysis.versions import Retired
from gr.api.deps import AppState, StateDep, UserDep, require_user
from gr.domain.initiative import Initiative
from gr.domain.process import Deadline
from gr.domain.review import ReviewEvent
from gr.errors import AppError
from gr.services.analyses import AnalysisService
from gr.services.reviews import ReviewIn, ReviewService
from gr.web.format import plain
from gr.web.labels import LABELS
from gr.web.routes import templates
from gr.web.settings import MAPS, SCALARS

router = APIRouter(dependencies=[Depends(require_user)], include_in_schema=False)

# How each correctable field is entered on the forms (overlay.EDITABLE; relations aside).
TEXT, GROUNDED, CHOICE, FLAG, LINES, DEADLINE, RATINGS = (
    "text", "grounded", "choice", "flag", "lines", "deadline", "ratings"
)  # fmt: skip
FORM_FIELDS: dict[str, dict[str, str]] = {
    "process": {"name": GROUNDED, "goal": GROUNDED, "trigger": GROUNDED, "result": GROUNDED},
    "operation": {
        "name": TEXT, "action": GROUNDED, "performer": GROUNDED, "object": TEXT,
        "deadline": DEADLINE, "operation_type": CHOICE, "legally_significant": FLAG,
        "handles_personal_data": FLAG, "is_loop_back": FLAG,
    },
    "decision_point": {"condition": GROUNDED},
    "finding": {"pattern": CHOICE, "description": TEXT},
    "initiative": {
        "title": TEXT, "description": TEXT, "architecture_idea": TEXT, "tech_rationale": TEXT,
        "solution_category": CHOICE, "automation_level": CHOICE, "ratings": RATINGS,
        "risks": LINES, "limitations": LINES,
    },
}  # fmt: skip
# A coded value in the log → the labels it is shown with.
CHOICES = {
    "operation_type": "operation_type",
    "pattern": "problem",
    "solution_category": "solution",
    "automation_level": "automation",
    "relation": "relation",
    "status": "gate",
}
MAX_TEXT = 4000
_ABSENT: Any = object()  # a group of inputs not on the form


def _text(form: Mapping[str, str], key: str) -> str | None:
    value = form.get(key)
    return None if value is None else value.strip()[:MAX_TEXT]


def _deadline(form: Mapping[str, str]) -> Any:
    """`{value, unit, anchor, raw}`, None for «no deadline», or _ABSENT."""
    unit = form.get("f:deadline.unit")
    if unit is None:
        return _ABSENT
    if not unit:
        return None
    number = (form.get("f:deadline.value") or "").strip()
    if number and not (number.isascii() and number.isdigit() and len(number) < 7):
        raise AppError("validation_error", "Срок: число — целое неотрицательное.")
    return {
        "value": int(number) if number else None,
        "unit": unit,
        "anchor": _text(form, "f:deadline.anchor") or None,
        "raw": _text(form, "f:deadline.raw") or None,
    }


def _deadline_of(current: Deadline | None) -> dict[str, Any] | None:
    if current is None:
        return None
    return {"value": current.value, "unit": current.unit.value, "anchor": current.anchor,
            "raw": current.raw.value}  # fmt: skip


def _ratings(form: Mapping[str, str], current: Any) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for criterion, rating in current.model_dump(mode="json").items():
        key = "bucket" if criterion == "duration" else "level"
        level = _text(form, f"f:ratings.{criterion}.{key}")
        why = _text(form, f"f:ratings.{criterion}.justification")
        new = {key: level or rating[key], "justification": rating["justification"] if why is None
               else why}  # fmt: skip
        if new != rating:
            found[criterion] = new
    return found


def changes(entity_type: str, item: Element, form: Mapping[str, str]) -> dict[str, Any]:
    """The patch of an edit form: only the fields whose value differs from the element's."""
    patch: dict[str, Any] = {}
    for name, kind in FORM_FIELDS[entity_type].items():
        current = getattr(item, name)
        if kind == TEXT:
            new = _text(form, f"f:{name}")
            if new is not None and (new or None) != current:
                patch[name] = new or None
        elif kind == GROUNDED:
            new, quote = _text(form, f"f:{name}"), _text(form, f"q:{name}")
            old = current.value if current is not None else None
            if quote:  # a basis given: a fact, if the act holds the quote
                patch[name] = {"value": new or old, "citation": {"quote": quote}}
            elif new is not None and (new or None) != old:
                patch[name] = new or None
        elif kind == CHOICE:
            new = _text(form, f"f:{name}")
            old = current
            if isinstance(item, Initiative) and name == "automation_level":
                old = item.automation_level_proposed  # shown on the form; the cap follows the rules
            if new and new != old:
                patch[name] = new
        elif kind == FLAG:
            flag = form.get(f"f:{name}")
            if flag in ("yes", "no") and (flag == "yes") != current:
                patch[name] = flag == "yes"
        elif kind == LINES:
            raw = form.get(f"f:{name}")
            lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
            if raw is not None and lines != current:
                patch[name] = lines
        elif kind == DEADLINE:
            deadline = _deadline(form)
            if deadline is not _ABSENT and deadline != _deadline_of(current):
                patch[name] = deadline
        elif kind == RATINGS and (rated := _ratings(form, current)):
            patch[name] = rated
    return patch


def _back(run_id: str, value: str | None) -> str:
    """Where the form was: a page of this analysis only, never another site."""
    if (
        value
        and value.startswith(f"/analyses/{run_id}/")
        and value.isprintable()
        and "//" not in value
        and "\\" not in value
    ):
        return value
    return f"/analyses/{run_id}/processes"


async def _decision(state: AppState, run_id: str, form: Mapping[str, str]) -> ReviewIn:
    entity_type, entity_id = form.get("entity_type", ""), form.get("entity_id", "")
    action, comment = form.get("action", ""), form.get("comment")
    patch: dict[str, Any] | None = None
    if action == "edit" and entity_type in FORM_FIELDS:
        outputs = await AnalysisService(state).outputs(run_id)
        item = outputs.effective.element(entity_type, entity_id)
        if item is None:
            raise AppError("not_found", "Элемент анализа не найден.")
        patch = changes(entity_type, item, form)
        if not patch:
            raise AppError("validation_error", "Изменений нет: значения совпадают с текущими.")
    elif action == "edit" and entity_type == "relation":
        patch = {"relation": form.get("f:relation", "")}
    elif action == "set_gate":
        status = form.get("f:status", "")
        if status == "blocked" and not (comment or "").strip():
            raise AppError("validation_error", "Запрет инициативы требует комментария.")
        patch = {"status": status}
    try:
        return ReviewIn.model_validate(
            {"entity_type": entity_type, "entity_id": entity_id, "action": action,
             "patch": patch, "comment": comment}
        )  # fmt: skip
    except ValidationError:
        raise AppError("validation_error", "Некорректное решение: обновите страницу.") from None


@router.post("/analyses/{run_id}/reviews", response_class=HTMLResponse)
async def decide(request: Request, run_id: str, state: StateDep, user: UserDep) -> Response:
    form = await request.form(max_files=0, max_fields=200)
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    back = _back(run_id, values.get("back"))
    try:
        await ReviewService(state).add(run_id, await _decision(state, run_id, values), user)
    except AppError as error:
        return templates.TemplateResponse(
            request, "error.html", {"error": error, "back": back}, status_code=error.status
        )
    return RedirectResponse(back, status_code=303)


# --- screen 7: history -----------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    field: str
    before: str
    after: str


@dataclass(frozen=True)
class Entry:
    event: ReviewEvent
    element: str
    link: str | None
    earlier: str | None  # «предыдущая версия N» for replaced initiatives and findings
    changes: list[Change]


@dataclass(frozen=True)
class Names:
    """How the elements of a run are called on the screens, replaced versions included."""

    run_id: str
    elements: dict[tuple[str, str], tuple[str, str]]  # (type, id) → (name, link)
    retired: dict[str, Retired]

    def name(self, entity_type: str, entity_id: str) -> str:
        if (entity_type, entity_id) in self.elements:
            return self.elements[(entity_type, entity_id)][0]
        retired = self.retired.get(entity_id)
        return retired.title if retired else entity_id


async def names_of(state: AppState, run_id: str) -> Names:
    try:
        outputs = await AnalysisService(state).outputs(run_id)
    except AppError:  # no process map yet: no decisions either
        return Names(run_id, {}, {})
    base = f"/analyses/{run_id}"
    found: dict[tuple[str, str], tuple[str, str]] = {}
    for p in outputs.effective.model.processes:
        here = f"{base}/processes?process={p.id}"
        found[("process", p.id)] = (p.name.value, here)
        for o in p.operations:
            found[("operation", o.id)] = (f"{o.seq + 1} · {o.name}", f"{here}#{o.id}")
        for d in p.decision_points:
            found[("decision_point", d.id)] = (d.condition.value, f"{here}#decisions")
    for f in outputs.effective.findings:
        found[("finding", f.id)] = (f.description, f"{base}/initiatives?process_id={f.process_id}")
    for i in outputs.effective.initiatives:
        found[("initiative", i.id)] = (i.title, f"{base}/initiatives/{i.id}")
    return Names(run_id, found, outputs.stored.retired())


def _number(value: Any) -> str:
    try:
        return plain(Decimal(str(value)))
    except InvalidOperation:
        return str(value)


def describe(field: str, value: Any) -> str:
    """A value of the log as the screens show it."""
    if value is None or value == "" or value == [] or value == {}:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if field in CHOICES and isinstance(value, str):
        return str(LABELS[CHOICES[field]].get(value, value))
    if field == "deadline" and isinstance(value, Mapping):
        raw = value.get("raw")
        raw = raw.get("value") if isinstance(raw, Mapping) else raw
        unit = LABELS["deadline_unit"].get(str(value.get("unit")), "")
        return str(
            raw or " ".join(str(x) for x in (value.get("value"), unit) if x not in (None, ""))
        )
    if field == "ratings" and isinstance(value, Mapping):
        parts = []
        for criterion, given in value.items():
            rating = given if isinstance(given, Mapping) else {}
            code = rating.get("bucket") or rating.get("level")
            label = LABELS["duration" if criterion == "duration" else "level"].get(str(code), code)
            why = rating.get("justification")
            parts.append(f"{LABELS['rating'].get(criterion, criterion)}: {label}"
                         + (f" ({why})" if why else ""))  # fmt: skip
        return "; ".join(parts)
    if isinstance(value, Mapping) and "value" in value:  # a value with its basis
        cited = value.get("citation") or value.get("citations")
        return describe(field, value["value"]) + (" (по цитате из НПА)" if cited else "")
    if isinstance(value, list):
        return "; ".join(describe(field, item) for item in value)
    return str(value)


def _assumption(names: Names, key: str) -> str:
    parameter = key.rsplit(".", 1)[-1]
    label = str(LABELS["assumption"].get(parameter, parameter))
    head, _, rest = key.partition(".")
    target = rest.rsplit(".", 1)[0] if "." in rest else ""
    if head == "proc":
        return f"{label} («{names.name('process', target)}»)"
    if head == "op":
        return f"{label}: {names.name('operation', target)}"
    if head == "ini":
        initiative, _, operation = target.partition(".")
        where = f", {names.name('operation', operation)}" if operation else ""
        return f"{label}: «{names.name('initiative', initiative)}»{where}"
    return label


def _numbers(value: Any) -> str:
    triple = value.get("values") if isinstance(value, Mapping) else None
    if not isinstance(triple, Mapping):
        return "—"
    return " / ".join(_number(triple.get(s)) for s in ("conservative", "base", "optimistic"))


def _assumption_changes(label: str, before: Any, after: Any) -> list[Change]:
    """Values by scenario; the comment only when the user wrote one (a profile's explanation
    goes away by itself when the number is changed)."""
    found = []
    if _numbers(before) != _numbers(after):
        found.append(Change(label, _numbers(before), _numbers(after)))
    note = after.get("note") if isinstance(after, Mapping) else None
    old = before.get("note") if isinstance(before, Mapping) else None
    if note and note != old:
        found.append(Change(f"{label}: комментарий", old or "—", note))
    return found


def _priority_value(name: str, value: Any) -> str:
    if isinstance(value, Mapping):
        codes = {"weights": "criterion", "level_map": "level", "duration_map": "duration"}
        names = LABELS.get(codes.get(name, ""), {})
        return ", ".join(f"{names.get(k, k)} {_number(v)}" for k, v in value.items())
    return "—" if value is None else _number(value)


def entry(event: ReviewEvent, names: Names) -> Entry:
    base = f"/analyses/{names.run_id}"
    patch, previous = event.patch or {}, event.previous or {}
    found: list[Change] = []
    link: str | None = None
    earlier: str | None = None
    if event.entity_type == "assumption_set":
        element, link = "Набор допущений", f"{base}/settings"
        found = [
            change
            for key, value in patch.items()
            for change in _assumption_changes(_assumption(names, key), previous.get(key), value)
        ]
    elif event.entity_type == "priority_config":
        element, link = "Веса и пороги приоритизации", f"{base}/settings#priority"
        titles = {k: v[0] for k, v in SCALARS.items()} | {k: v[1] for k, v in MAPS.items()}
        found = [Change(titles.get(name, name), _priority_value(name, previous.get(name)),
                        _priority_value(name, value)) for name, value in patch.items()]  # fmt: skip
    else:
        if event.entity_type == "relation":
            element = " ↔ ".join(
                f"«{names.name('initiative', k)}»" for k in event.entity_id.split("|")
            )
        else:
            element = names.name(event.entity_type, event.entity_id)
            known = names.elements.get((event.entity_type, event.entity_id))
            link = known[1] if known else None
            retired = names.retired.get(event.entity_id)
            if known is None and retired is not None:
                earlier = f"предыдущая версия {retired.version}"
        if event.action in ("edit", "set_gate"):
            found = [
                Change(str(LABELS["field"].get(name, name)), describe(name, previous.get(name)),
                       describe(name, value))
                for name, value in patch.items()
            ]  # fmt: skip
    return Entry(event, element, link, earlier, found)


@router.get("/analyses/{run_id}/history", response_class=HTMLResponse)
async def history(
    request: Request,
    run_id: str,
    state: StateDep,
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: Annotated[str | None, Query()] = None,
    user: Annotated[str | None, Query()] = None,
) -> Response:
    try:
        run = await AnalysisService(state).get(run_id)
        events = await ReviewService(state).history(run_id)
    except AppError as error:
        return templates.TemplateResponse(
            request, "error.html", {"error": error}, status_code=error.status
        )
    names = await names_of(state, run_id)
    shown = [
        e
        for e in events
        if (not entity_type or e.entity_type == entity_type)
        and (not entity_id or e.entity_id == entity_id or entity_id in e.entity_id.split("|"))
        and (not user or e.user == user)
    ]
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "run": run,
            "entries": [entry(e, names) for e in shown],
            "users": sorted({e.user for e in events}),
            "entity_type": entity_type or "",
            "entity_id": entity_id or "",
            "element": names.name(entity_type or "", entity_id) if entity_id else None,
            "user": user or "",
        },
    )
