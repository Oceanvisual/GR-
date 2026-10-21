# API Contracts: AI-анализ НПА

**Feature**: [../spec.md](../spec.md) · **Models**: [../data-model.md](../data-model.md)

REST JSON, префикс `/api/v1`. Все маршруты, кроме `/healthz`, закрыты HTTP Basic (R-15). OpenAPI
генерируется FastAPI (`/api/v1/openapi.json`) и сверяется contract-тестами с этим документом.
Ответы — Pydantic-модели из `data-model.md`. Вычисляемые блоки (экономика, приоритет) строятся
при чтении из текущих допущений и конфигурации.

## Общие соглашения

- Идентификаторы: `doc_…`, `run_…`, `proc_…`, `ini_…`.
- Даты: ISO 8601 UTC. Деньги: строка-decimal (`"1234567.89"`), чтобы не терять точность.
- Пагинация в MVP не нужна: объёмы малы. Списки отдаются целиком.
- Оптимистическая блокировка для версионируемых ресурсов (допущения, конфигурация приоритета):
  заголовок `If-Match: <version>`. При несовпадении — `409 version_conflict`.

### Формат ошибки
```json
{"error": {"code": "no_text_layer", "message": "В PDF нет текстового слоя: требуется OCR.",
           "details": {"empty_pages": [1, 2, 3]}}}
```

| HTTP | `code` | Когда |
|---|---|---|
| 400 | `validation_error` | невалидное тело или параметры |
| 401 | `unauthorized` | нет или неверная аутентификация |
| 403 | `csrf_rejected` | изменяющий запрос браузера с чужого источника (R-15) |
| 404 | `not_found` | |
| 409 | `version_conflict`, `run_in_progress`, `ocr_in_progress` | |
| 413 | `file_too_large`, `document_too_long` | превышены лимиты (FR-002) |
| 415 | `unsupported_format` | `details.supported` — список форматов |
| 422 | `corrupted_file`, `encrypted_file`, `no_text_layer`, `empty_document`, `encoding_undetected`, `document_not_analyzable` | FR-003, FR-004 |
| 503 | `llm_not_configured`, `external_llm_not_allowed` | NFR-009, проверка до запуска |
| 500 | `internal_error` | без деталей реализации |

Ошибки этапов анализа (`llm_unavailable`, `llm_timeout`, `llm_invalid_output`,
`stage_failed`) не возвращаются HTTP-ошибкой. Они видны в `AnalysisRun.stages[].error`.

## Здоровье

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/healthz` | `200 {"status": "ok"}` — процесс жив (без аутентификации, для Docker) |
| GET | `/api/v1/readyz` | `200 {"db": "ok", "llm": {"policy": "local_only", "configured": true}, "ocr": {"available": true, "langs": ["rus", "eng"]}, "version": "0.1.0"}` |

## Документы

### `POST /api/v1/documents` — загрузка (FR-001…FR-006)
`multipart/form-data`: `file` (обязательно), `source_note?`.
- `201 Document`: разбор и построение структуры выполнены синхронно (SC-001: ≤ 30 с).
- `202 Document`: в PDF есть страницы без текста и OCR доступен. Распознавание поставлено в
  фоновую очередь (`diagnostics.ocr.status=queued`), клиент опрашивает `GET /documents/{id}`.
- `200 Document` + заголовок `X-Duplicate-Of: doc_…`: такой файл уже загружен.
- Ошибки: `413`, `415`, `422` (см. таблицу). Если OCR недоступен или завершился ошибкой,
  документ со статусом `needs_ocr` **сохраняется** (`201`, `diagnostics.analyzable=false`),
  чтобы пользователь видел диагностику. Запуск анализа для него вернёт
  `422 document_not_analyzable`, а во время OCR — `409 ocr_in_progress`.

### `POST /api/v1/documents/{doc_id}/ocr` — повторить OCR
Только при `diagnostics.ocr.status = failed`. Ответ — `202 Document`.

### `GET /api/v1/documents` → `list[DocumentSummary]`
`DocumentSummary = {id, filename, format, uploaded_at, metadata.title, diagnostics.text_layer,
last_run?: {id, status}}`.

### `GET /api/v1/documents/{doc_id}` → `Document`

### `PATCH /api/v1/documents/{doc_id}/metadata`
Тело: `{fields?: {<meta_field>: str | null}, user_source?, relevance_checked_at?, relevance_note?}`.
Ответ — `200 Document`. Изменения записываются в журнал с `source="user"`.

### `GET /api/v1/documents/{doc_id}/structure` → `StructureTree`
`{mode: StructureMode, stats: {sections, points, subpoints, pages?, chars}, nodes: list[FragmentNode]}`,
где `FragmentNode = Fragment − text + {children: list[str], text_preview: str}`.

### `GET /api/v1/documents/{doc_id}/fragments/{fragment_id}` → `FragmentWithContext`
`{fragment: Fragment, path: [{id, label, heading}], parent?, previous?, next?}`. Id фрагмента — путь со слешами
(`body/III/3.5/3.5.1`). Query `?with_context=true` заполняет путь заголовков (предки ниже корня), родителя и соседние фрагменты
(FR-072).

## Анализ

### `POST /api/v1/documents/{doc_id}/analyses` — запуск (US2)
Тело: `{}` (параметры берутся из конфигурации сервиса). Ответ — `202 AnalysisRun`
(`status=queued`). Проверки до постановки в очередь: документ анализируем, политика LLM
разрешает endpoint (иначе `503`), нет активного запуска по этому документу (иначе
`409 run_in_progress`).

### `GET /api/v1/analyses/{run_id}` → `AnalysisRun`
Опрос статуса. UI опрашивает его через HTMX каждые 2 с, пока статус `queued` или `running`.

### `POST /api/v1/analyses/{run_id}/resume` → `202 AnalysisRun`
Только из `failed` или `interrupted`. Возобновляет с первого незавершённого этапа.

### `POST /api/v1/analyses/{run_id}/cancel` → `200 AnalysisRun`

### `GET /api/v1/documents/{doc_id}/analyses` → `list[AnalysisRunSummary]`
Версии анализа (FR-084).

### `GET /api/v1/analyses/{run_id}/processes` → `ProcessMap`
`{processes: list[Process], coverage: CoverageReport, unverified: list[UnverifiedItem]}`.
Модель уже с наложенными решениями эксперта. `UnverifiedItem = {element_type, element_id,
process_id, value, citation, reason}`.

### `GET /api/v1/analyses/{run_id}/processes/{process_id}` → `Process`
Плюс `mermaid: str` — детерминированно сгенерированная блок-схема (экранированные подписи).

### `POST /api/v1/analyses/{run_id}/processes/{process_id}/regenerate-initiatives` → `202 AnalysisRun`
Явная перегенерация устаревших (`stale`) инициатив процесса (FR-083). Создаёт новую версию
вывода этапа S6 для процесса, старая сохраняется.

## Инициативы, экономика, приоритет

Query-параметр `scenario` во всех запросах этого раздела: `conservative | base | optimistic`
(по умолчанию `base`).

### `GET /api/v1/analyses/{run_id}/initiatives` → `InitiativeTable`
```text
{scenario, assumptions_version, priority_config_version,
 rows: list[{initiative: Initiative, process_name, finding_patterns,
             economics: InitiativeEconomics, priority: PriorityResult}],
 portfolio: PortfolioEconomics,
 banners: list[str]}          # «модельные допущения», «объёмы не заданы» и т. п.
```
Сортировка: неблокированные по `priority.rank`, затем `blocked`. Фильтры: `group`, `process_id`,
`gate`, `include_rejected=false`.

### `GET /api/v1/analyses/{run_id}/initiatives/{initiative_id}` → `InitiativeDetail`
Всё для экрана 5 (FR-070): инициатива, процесс и операции с цитатами, проблемы, экономика по
трём сценариям, использованные допущения (ключи и значения), разбивка приоритета, статус
допустимости, связи, история решений по элементу.

### `GET /api/v1/analyses/{run_id}/economics` → `EconomicsResult`

### `GET /api/v1/analyses/{run_id}/assumptions` → `AssumptionSet` (текущая версия)
Заголовок `ETag: <version>`.

### `PATCH /api/v1/analyses/{run_id}/assumptions` (заголовок `If-Match`)
Тело: `{changes: list[{key, values?: Triple, note?}], comment?}`. Создаётся новая версия, в
журнал пишется `ReviewEvent(entity_type="assumption_set")`. Ответ — `200 {assumption_set,
initiatives: InitiativeTable}`: сразу пересчитанная таблица, ни одного вызова LLM (SC-005).
Валидация FR-057: `400 validation_error` с перечнем ключей. Нелогичный порядок сценариев
возвращается в `warnings`, а не ошибкой.

### `GET /api/v1/analyses/{run_id}/priority-config` → `PriorityConfig` (+ `ETag`)

### `PUT /api/v1/analyses/{run_id}/priority-config` (заголовок `If-Match`)
Тело: `PriorityConfig`. Ответ — `200 {config, initiatives: InitiativeTable}`.

## Экспертная проверка (US6)

### `POST /api/v1/analyses/{run_id}/reviews`
Тело: `{entity_type, entity_id, action, patch?, comment?}`. Правила:
- для `reject` требуется `comment`;
- `patch` — только поля из белого списка типа сущности, иначе `400`;
- для `set_gate` поле `patch.status` должно быть `LegalGate`.

Ответ — `201 {event: ReviewEvent, entity: <обновлённая сущность>, stale_initiative_ids: list[str]}`.

### `GET /api/v1/analyses/{run_id}/reviews` → `list[ReviewEvent]`
Фильтры: `entity_type`, `entity_id`, `user`. Сортировка по `created_at` по убыванию.

## Экспорт (US7)

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/api/v1/analyses/{run_id}/export.json?scenario=` | `application/json`, `Content-Disposition: attachment` — `AnalysisExport` |
| GET | `/api/v1/analyses/{run_id}/export/initiatives.csv?scenario=` | CSV UTF-8 с BOM, разделитель `;` |
| GET | `/api/v1/analyses/{run_id}/export/operations.csv` | CSV |
| GET | `/api/v1/schema/analysis-export.json` | JSON Schema `AnalysisExport` (из Pydantic) |

### `AnalysisExport` (schema_version `1.0`)
```text
{schema_version, exported_at, exported_by, scenario,
 document: Document, structure: list[Fragment],
 run: AnalysisRun (config_snapshot, usage),
 processes: list[Process], coverage, unverified,
 findings: list[Finding], initiatives: list[Initiative],
 assumptions: AssumptionSet, priority_config: PriorityConfig,
 economics: {conservative, base, optimistic: EconomicsResult},
 priority: list[PriorityResult],
 reviews: list[ReviewEvent],
 disclaimers: list[str]}
```
У каждого элемента есть `layer` и `review.status` (FR-093).

### `initiatives.csv` — колонки
`rank;group;gate;score;initiative_id;title;process;operations;problem_patterns;solution_category;
regulatory_mode;automation_level;hours_per_year_y1;fte_y1;money_equiv_y1;capex;opex_y1;net_y1;
cum_net_horizon;roi_horizon;payback_months;in_portfolio;review_status;scenario;assumptions_version`

### `operations.csv` — колонки
`process_id;process_name;operation_id;seq;name;performer;action;operation_type;deadline_raw;
legally_significant;fragment_labels;quotes;grounding;confidence;review_status`

## Mermaid-генерация (детерминированная)
`flowchart TD`. Узлы `op_<n>["<seq>. <name> — <performer>"]` (кавычки и спецсимволы
экранируются, длина ≤ 80). Ромбы — для точек решения. Пунктирные рёбра — для
`is_loop_back`. Отклонённые операции не выводятся. Генерация — чистая функция с
golden-тестом.
