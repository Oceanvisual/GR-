"""Russian labels for statuses shown on the screens."""

from gr.domain.enums import OcrStatus, RunStatus, TextLayerStatus

METADATA_FIELDS: dict[str, str] = {
    "act_type": "Вид акта",
    "issuer": "Орган",
    "date": "Дата",
    "number": "Номер",
    "title": "Наименование",
    "edition_date": "Редакция от",
    "minjust_registration": "Регистрация в Минюсте",
}

TEXT_LAYER: dict[str, str] = {
    TextLayerStatus.OK: "текстовый слой есть",
    TextLayerStatus.PARTIAL: "часть страниц без текста",
    TextLayerStatus.NEEDS_OCR: "скан: нужен OCR",
    TextLayerStatus.EMPTY: "текста нет",
}

OCR: dict[str, str] = {
    OcrStatus.NOT_NEEDED: "не нужен",
    OcrStatus.QUEUED: "в очереди",
    OcrStatus.RUNNING: "выполняется",
    OcrStatus.DONE: "выполнен",
    OcrStatus.FAILED: "ошибка",
    OcrStatus.UNAVAILABLE: "недоступен",
}

RUN: dict[str, str] = {
    RunStatus.QUEUED: "в очереди",
    RunStatus.RUNNING: "выполняется",
    RunStatus.SUCCEEDED: "завершён",
    RunStatus.FAILED: "ошибка",
    RunStatus.INTERRUPTED: "прерван",
    RunStatus.CANCELLED: "отменён",
}

STRATEGY: dict[str, str] = {
    "regulation": "нумерация пунктов и статей",
    "layout": "заголовки по оформлению",
    "paragraphs": "абзацы без нумерации",
}

STAGE: dict[str, str] = {
    "discover": "S3 · карта процессов",
    "detail": "S4 · операции",
    "validate": "S5 · проверка",
    "opportunities": "S6 · возможности",
    "postprocess": "S7 · проверка инициатив",
}

STAGE_STATUS: dict[str, str] = {
    "pending": "ожидает",
    "running": "выполняется",
    "succeeded": "готово",
    "failed": "ошибка",
    "skipped": "пропущен",
}

LAYER: dict[str, str] = {
    "fact": "Факт",
    "interpretation": "Интерпретация",
    "hypothesis": "Гипотеза",
    "assumption": "Допущение",
}

CONFIDENCE: dict[str, str] = {"high": "высокая", "medium": "средняя", "low": "низкая"}

OPERATION_TYPE: dict[str, str] = {
    "intake": "приём",
    "registration": "регистрация",
    "completeness_check": "проверка комплектности",
    "data_verification": "проверка сведений",
    "interagency_request": "межведомственный запрос",
    "document_preparation": "подготовка документа",
    "approval": "согласование",
    "decision": "решение",
    "signing": "подписание",
    "notification": "уведомление",
    "issuance": "выдача",
    "record_keeping": "учёт",
    "control": "контроль",
    "other": "прочее",
}

DEADLINE_UNIT: dict[str, str] = {
    "minutes": "мин.",
    "hours": "ч",
    "working_days": "раб. дн.",
    "calendar_days": "кал. дн.",
    "days": "дн.",
    "months": "мес.",
    "next_working_day": "следующий рабочий день",
    "other": "",
}

UNVERIFIED_ELEMENT: dict[str, str] = {
    "process_name": "название процесса",
    "goal": "цель",
    "trigger": "запуск",
    "result": "результат",
    "participant": "участник",
    "input": "входной документ",
    "information_system": "информационная система",
    "deadline": "срок",
    "operation": "операция",
    "performer": "исполнитель",
    "condition": "условие",
    "decision_point": "точка решения",
    "criterion": "критерий",
}

GROUNDING: dict[str, str] = {
    "verified": "цитата найдена",
    "relocated": "ссылка исправлена",
    "partial": "близкое совпадение",
    "unverified": "не найдена",
    "invalid_quote": "некорректная цитата",
}

LABELS = {
    "metadata_fields": METADATA_FIELDS,
    "text_layer": TEXT_LAYER,
    "ocr": OCR,
    "run": RUN,
    "strategy": STRATEGY,
    "stage": STAGE,
    "stage_status": STAGE_STATUS,
    "layer": LAYER,
    "confidence": CONFIDENCE,
    "operation_type": OPERATION_TYPE,
    "deadline_unit": DEADLINE_UNIT,
    "unverified_element": UNVERIFIED_ELEMENT,
    "grounding": GROUNDING,
}

LABELS.update(
    {
        "gate": {
            "allowed": "Допустимо",
            "allowed_with_hitl": "Контроль человеком",
            "requires_regulatory_change": "Нужно изменение НПА",
            "blocked": "Заблокировано",
        },
        "solution": {
            "classic_automation": "Классическая автоматизация",
            "ai_nlp": "AI/NLP",
            "llm_rag": "LLM + RAG",
            "ai_agent": "AI-агент",
            "hybrid": "Гибрид",
            "no_change": "Оптимизация не требуется",
        },
        "regulatory": {
            "within_current": "В рамках действующего регулирования",
            "requires_change": "Требует изменения НПА",
        },
        "automation": {
            "assistive": "Подсказка",
            "human_approval": "Решение подтверждает человек",
            "autonomous": "Автономно",
        },
        "level": {
            "very_low": "Очень низкий",
            "low": "Низкий",
            "medium": "Средний",
            "high": "Высокий",
            "very_high": "Очень высокий",
        },
        "duration": {
            "lt_3m": "до 3 месяцев",
            "m3_6": "3–6 месяцев",
            "m6_12": "6–12 месяцев",
            "gt_12m": "более 12 месяцев",
        },
        "rating": {
            "feasibility": "Реализуемость",
            "data_availability": "Доступность данных",
            "integration_complexity": "Сложность интеграции",
            "legal_risk": "Юридический риск",
            "operational_risk": "Операционный риск",
            "duration": "Срок внедрения",
        },
        "problem": {
            "manual_document_processing": "Ручная обработка документов",
            "duplicate_data_entry": "Повторный ввод",
            "lookup_and_reconciliation": "Поиск и сверка",
            "request_classification": "Классификация обращений",
            "template_document_preparation": "Типовые документы",
            "duplicate_checks": "Повторные проверки",
            "long_approvals": "Длительные согласования",
            "interdepartmental_handoff": "Передача документов",
            "large_text_analysis": "Анализ большого объёма текста",
            "other": "Иное",
        },
    }
)
