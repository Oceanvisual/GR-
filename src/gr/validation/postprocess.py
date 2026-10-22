"""S7: referential integrity, conservative defaults and deterministic initiative rules."""

from collections.abc import Sequence

from pydantic import ValidationError

from gr.analysis.gating import cap_autonomy, compute_gate
from gr.analysis.relations import infer_relations
from gr.analysis.tech_guard import evaluate
from gr.domain.document import Fragment
from gr.domain.enums import Level5, RegulatoryMode, SolutionCategory
from gr.domain.initiative import (
    Finding,
    Initiative,
    InitiativeSet,
    NoChangeDecision,
    PostprocessIssue,
)
from gr.domain.process import ProcessModel
from gr.extraction.opportunities import OpportunitiesResult, effective_process
from gr.extraction.render import FragmentAliases, in_force


def postprocess(
    model: ProcessModel, drafts: OpportunitiesResult, fragments: Sequence[Fragment]
) -> InitiativeSet:
    result = InitiativeSet()
    aliases = FragmentAliases(fragments)
    valid_fragments = in_force(fragments)
    by_process = {p.id: p for p in model.processes}
    seen: set[str] = set()

    def issue(code: str, process_id: str, element: str, message: str) -> None:
        result.issues.append(
            PostprocessIssue(
                code=code,
                process_id=process_id,
                element=element,
                message=message,
            )
        )

    order = {p.id: n for n, p in enumerate(model.processes)}
    for draft in sorted(drafts.processes, key=lambda d: order.get(d.process_id, len(order))):
        pid = draft.process_id
        if pid not in by_process or pid in seen:
            issue("invalid_process", pid, pid, "Неизвестный или повторный процесс в ответе S6.")
            continue
        seen.add(pid)
        process = effective_process(by_process[pid])
        if process is None:
            continue
        operation_ids = {op.id for op in process.operations}
        finding_ids: dict[str, str] = {}
        for raw_finding in draft.answer.findings:
            if raw_finding.key in finding_ids:
                issue("duplicate_key", pid, raw_finding.key, "Повторный ключ проблемы отброшен.")
                continue
            ops = list(dict.fromkeys(k for k in raw_finding.operation_ids if k in operation_ids))
            if set(raw_finding.operation_ids) - operation_ids:
                issue(
                    "unknown_operation",
                    pid,
                    raw_finding.key,
                    "Несуществующие ссылки на операции удалены.",
                )
            if not ops:
                issue(
                    "empty_finding",
                    pid,
                    raw_finding.key,
                    "Проблема без существующих операций исключена.",
                )
                continue
            key = f"fnd_{pid}_{len(finding_ids) + 1}"
            finding_ids[raw_finding.key] = key
            result.findings.append(
                Finding(
                    id=key,
                    process_id=pid,
                    operation_ids=ops,
                    pattern=raw_finding.pattern,
                    description=raw_finding.description,
                )
            )

        local_keys: set[str] = set()
        start = len(result.initiatives)
        no_change_reason = (
            draft.answer.no_change.justification.strip() if draft.answer.no_change.applies else ""
        )
        for raw in draft.answer.initiatives:
            if raw.key in local_keys:
                issue("duplicate_key", pid, raw.key, "Повторный ключ инициативы отброшен.")
                continue
            local_keys.add(raw.key)
            ops = list(dict.fromkeys(k for k in raw.operation_ids if k in operation_ids))
            if set(raw.operation_ids) - operation_ids:
                issue(
                    "unknown_operation", pid, raw.key, "Несуществующие ссылки на операции удалены."
                )
            if not ops:
                issue(
                    "empty_initiative",
                    pid,
                    raw.key,
                    "Инициатива без существующих операций исключена.",
                )
                continue
            if raw.solution_category == SolutionCategory.NO_CHANGE:
                no_change_reason = raw.tech_rationale.strip() or raw.description.strip()
                continue
            linked = list(
                dict.fromkeys(finding_ids[k] for k in raw.finding_keys if k in finding_ids)
            )
            if not linked:
                issue(
                    "missing_finding",
                    pid,
                    raw.key,
                    "Инициатива не связана с существующей проблемой.",
                )
                continue
            if any(k not in finding_ids for k in raw.finding_keys):
                issue("unknown_finding", pid, raw.key, "Несуществующие ссылки на проблемы удалены.")
            reductions = {}
            for reduction in raw.reduction_potential:
                if reduction.operation_id in ops and reduction.operation_id not in reductions:
                    reductions[reduction.operation_id] = reduction.model_dump(mode="json")
            for op in ops:
                if op not in reductions:
                    issue(
                        "missing_reduction",
                        pid,
                        raw.key,
                        "Отсутствующий потенциал заменён на very_low.",
                    )
                    reductions[op] = {
                        "operation_id": op,
                        "level": Level5.VERY_LOW.value,
                        "justification": "Потенциал не задан; very_low требует проверки.",
                    }
            provisions = []
            for provision in raw.provisions_to_revise:
                fid = (
                    provision.ref
                    if provision.ref in valid_fragments
                    else aliases.resolve(provision.ref)
                )
                if fid is None or fid not in valid_fragments:
                    issue(
                        "unknown_fragment",
                        pid,
                        raw.key,
                        "Неизвестное положение к пересмотру удалено.",
                    )
                    continue
                provisions.append({"fragment_id": fid, "change_nature": provision.change_nature})
            if raw.regulatory_mode == RegulatoryMode.REQUIRES_CHANGE and not provisions:
                issue(
                    "missing_provision",
                    pid,
                    raw.key,
                    "Нет положения к пересмотру. Нужна юридическая экспертиза.",
                )
                continue
            values = raw.model_dump(
                mode="json", exclude={"key", "finding_keys", "provisions_to_revise"}
            )
            values.update(
                {
                    "id": f"ini_{pid}_{len(result.initiatives) - start + 1}",
                    "process_id": pid,
                    "finding_ids": linked,
                    "operation_ids": ops,
                    "reduction_potential": [reductions[op] for op in ops],
                    "provisions_to_revise": provisions,
                    "requires_legal_review": raw.regulatory_mode == RegulatoryMode.REQUIRES_CHANGE,
                    "automation_level_proposed": raw.automation_level,
                }
            )
            try:
                initiative = Initiative.model_validate(values)
            except ValidationError:
                issue(
                    "invalid_initiative",
                    pid,
                    raw.key,
                    "Инициатива нарушает обязательные поля; исходное предложение сохранено в S6.",
                )
                continue
            initiative = cap_autonomy(initiative, process.operations)
            initiative = initiative.model_copy(
                update={
                    "tech_guard": evaluate(
                        initiative, result.findings, operations=process.operations
                    ),
                    "gate": compute_gate(initiative, process.operations),
                }
            )
            result.initiatives.append(initiative)
        if len(result.initiatives) == start and no_change_reason:
            result.no_change.append(
                NoChangeDecision(process_id=pid, justification=no_change_reason)
            )
        elif len(result.initiatives) == start:
            issue(
                "no_result",
                pid,
                pid,
                "Нет валидных инициатив и обоснованного вывода об отсутствии оптимизации.",
            )
        elif no_change_reason:
            issue(
                "contradictory_no_change",
                pid,
                pid,
                "Вывод об отсутствии оптимизации противоречит инициативам.",
            )
    return result.model_copy(update={"initiatives": infer_relations(result.initiatives)})
