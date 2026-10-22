"""Evidence packet for a human expert: golden annotation, omissions and source text (T036)."""

from collections.abc import Sequence

from evals.golden import Golden
from evals.metrics import DocumentScore, Prediction
from gr.domain.document import Fragment
from gr.extraction.discovery import DiscoveryResult
from gr.extraction.render import with_subtrees


def _cell(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def review_markdown(
    golden: Golden,
    fragments: Sequence[Fragment],
    discovery: DiscoveryResult,
    prediction: Prediction,
    score: DocumentScore,
) -> str:
    by_label = {f.label: f for f in fragments}
    matched = {m.golden: m for m in score.matches}
    lines = [
        f"# Экспертная проверка: {_cell(golden.document)}",
        "",
        f"SHA-256 исходного файла: `{golden.sha256}`.",
        f"Разметил: {_cell(golden.annotated_by)}. "
        f"Проверил: {_cell(golden.verified_by or 'НЕ ПРОВЕРЕНО')}.",
        "Результаты ниже рассчитаны офлайн по сохранённым ответам. Это черновая разметка,",
        "её полнота и выбранное деление на процессы требуют решения эксперта.",
        "",
        "## Что считать процессом",
        "",
        golden.definition,
        "",
        "Эксперту: подтвердить или исправить границы процессов, обязательность, ключевые действия",
        "и сроки. Для каждого изменения указать пункт, обоснование и имя проверяющего.",
        "",
        "## Лишние процессы по текущему эталону",
        "",
    ]
    for name in score.extra:
        predicted = next(p for p in prediction.processes if p.name == name)
        lines += [f"### {_cell(name)}", "", "Цитируемые фрагменты и их собственный текст:", ""]
        for label in predicted.labels:
            text = by_label[label].text if label in by_label else "Фрагмент не найден"
            lines.append(f"- **{_cell(label)}**: {_cell(text)}")
        lines += ["", "Решение эксперта: ___; основание: ___", ""]
    if not score.extra:
        lines += ["Нет.", ""]

    lines += ["## Сверка эталона с исходным текстом", ""]
    for process in golden.processes:
        match = matched.get(process.key)
        candidate = next(
            (p for p in discovery.processes if match and p.name == match.predicted), None
        )
        context = (
            None
            if candidate is None
            else {
                f.id
                for f in with_subtrees(
                    fragments, [*candidate.core_fragment_ids, *candidate.related_fragment_ids]
                )
                if f.text
            }
        )
        missed = set(match.missed_operations) if match else {o.name for o in process.operations}
        lines += [
            f"### `{process.key}` — {_cell(process.name)}",
            "",
            f"Обязательный: {'да' if process.required else 'нет'}. Корни: "
            + "; ".join(_cell(r) for r in process.roots)
            + ".",
            "Найден: " + (_cell(match.predicted) if match else "нет") + ".",
            "",
        ]
        if process.operations:
            lines += [
                "| Ключевое действие эталона | Оценка | Пункт и исходный текст |",
                "|---|---|---|",
            ]
        for operation in process.operations:
            if operation.name not in missed:
                status = "сопоставлено по цитируемому фрагменту"
            elif context is None:
                status = "не сопоставлено; контекст не определён"
            elif all(by_label[label].id not in context for label in operation.labels):
                status = "не сопоставлено; исходный пункт не передан в S4"
            else:
                status = "не сопоставлено; исходный пункт передан в S4"
            evidence = " / ".join(f"{label}: {by_label[label].text}" for label in operation.labels)
            lines.append(f"| {_cell(operation.name)} | {status} | {_cell(evidence)} |")
        if process.deadlines:
            lines += ["", "Сроки эталона:", ""]
            for deadline in process.deadlines:
                lines.append(
                    f"- {deadline.value if deadline.value is not None else 'без числа'} "
                    f"`{deadline.unit}` — {_cell(deadline.what)}; **{_cell(deadline.label)}**: "
                    f"{_cell(by_label[deadline.label].text)}"
                )
        lines += ["", "Решение эксперта: ___; исправления и основание: ___", ""]
    lines += [
        "## Ограничения автоматической оценки",
        "",
        "Сопоставление действий по общему фрагменту не доказывает семантическое совпадение.",
        "Отметка «пункт передан в S4» означает наличие текста в общем контексте процесса;",
        "она не утверждает, что ответ модели полностью описывает пункт. Решение эксперта",
        "не подменяется этой таблицей. Значение verified_by остаётся без изменений.",
        "",
    ]
    return "\n".join(lines)
