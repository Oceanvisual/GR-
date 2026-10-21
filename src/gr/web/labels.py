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

LABELS = {
    "metadata_fields": METADATA_FIELDS,
    "text_layer": TEXT_LAYER,
    "ocr": OCR,
    "run": RUN,
    "strategy": STRATEGY,
}
