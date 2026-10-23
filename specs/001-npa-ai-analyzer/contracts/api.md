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
  `GET` отдаёт `ETag: "<version>"`, изменяющий запрос обязан прислать `If-Match` с этой
  версией (принимаются `"4"`, `4` и `W/"4"`; без заголовка — `400 validation_error`). При
  несовпадении — `409 version_conflict` с `details.current_version`.
- OpenAPI описывает ошибки диапазонами `4XX` и `5XX` со схемой `ErrorResponse` (формат ниже):
  невалидный запрос — `400 validation_error`, а не `422` FastAPI. Contract-тест
  (`tests/contract/test_openapi_contract.py`, T055) сверяет маршруты, методы и первый
  названный здесь код успеха с OpenAPI и проверяет `401` без входа на каждом маршруте
  `/api/v1`; маршрут, который появится в будущей задаче, перечислен в нём явно.

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
При `diagnostics.ocr.status = failed`, а также `unavailable`, если движок появился после
перезапуска сервиса. Ответ — `202 Document` (`ocr.status=queued`, `ocr.progress`). `400
validation_error`: OCR не нужен, уже идёт или выполнен; движок недоступен; по документу уже
запускался анализ (новый текст разошёлся бы с его цитатами). Прерванный остановкой сервиса OCR
при следующем старте возвращается в очередь сам.

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
Только из `failed` или `interrupted` (иначе `400 validation_error`). Возобновляет с первого
незавершённого этапа; в S4 повторно обрабатываются только процессы без сохранённого результата.
Политика LLM проверяется заново (`503`).

### `POST /api/v1/analyses/{run_id}/cancel` → `200 AnalysisRun`
Завершённый анализ отменить нельзя: `400 validation_error`.

### `GET /api/v1/documents/{doc_id}/analyses` → `list[AnalysisRunSummary]`
Версии анализа (FR-084), новые сначала: `{id, status, created_at, created_by,
stages: [{name, status}], usage, completeness}`. Перегенерации инициатив (T052) — не версии
анализа: их здесь нет.

Этапы, которых ещё нет в сборке (S6, S7 до US3), у завершённого запуска получают статус
`skipped`. Для активного запуска `usage` считается на лету; `usage.cost` — по `LLM_PRICE_*`,
если обе цены заданы.

### `GET /api/v1/analyses/{run_id}/processes` → `ProcessMap`
`{processes: list[Process], coverage: CoverageReport, unverified: list[UnverifiedItem]}`.
Модель уже с наложенными решениями эксперта. `UnverifiedItem = {element_type, element_id,
process_id, value, citation, reason}`.

### `GET /api/v1/analyses/{run_id}/processes/{process_id}` → `Process`
Плюс `mermaid: str` — детерминированно сгенерированная блок-схема (экранированные подписи,
T033). Пока карта не готова (этап S5 не завершён) — `404 not_found`.

### `POST /api/v1/analyses/{run_id}/processes/{process_id}/regenerate-initiatives` → `202 AnalysisRun`
Явная перегенерация инициатив процесса (FR-083), обычно устаревших (`stale`). Создаёт
запуск-перегенерацию (`parent_run_id`, `process_id`; S3 и S4 — `skipped`): его S5 — процесс
с решениями эксперта, S6 и S7 — только по этому процессу. Статус — `GET /analyses/{id}`
перегенерации, «Продолжить» и «Отменить» — как у анализа; её результаты читаются в анализе
(маршруты результатов по её id — `404` с `details.run_id` анализа). Успешная перегенерация
даёт следующую версию инициатив процесса, она становится действующей; прежние сохраняются.
Ошибки: анализ или процесс не найден, инициативы не готовы — `404`; процесс или все его
операции отклонены — `400`; запуск по документу уже идёт (анализ или перегенерация; то же
для `resume`) — `409 run_in_progress` с `details.run_id`; политика LLM — как при запуске.

### `GET /api/v1/analyses/{run_id}/processes/{process_id}/initiative-versions` → `list[ProcessVersion]`
Все версии инициатив процесса, первая первой: `{version, run_id (анализ или перегенерация),
created_at, current, result: InitiativeSet (как сохранил S7), predecessors: dict[новая →
заменённая], without_successor}` (T052).

## Инициативы, экономика, приоритет

Query-параметр `scenario` во всех запросах этого раздела: `conservative | base | optimistic`
(по умолчанию `base`).

### `GET /api/v1/analyses/{run_id}/initiatives` → `InitiativeTable`
```text
{scenario, assumptions_version, priority_config_version,
 rows: list[{initiative: Initiative, process_name, finding_patterns,
             economics: InitiativeEconomics, priority: PriorityResult,
             excluded_reason}],   # вне расчёта по решениям эксперта (T051)
 portfolio: PortfolioEconomics,
 banners: list[str]}          # «модельные допущения», «объёмы не заданы» и т. п.
```
Сортировка: неблокированные по `priority.rank`, затем `blocked`, затем вне расчёта. Фильтры:
`group`, `process_id`, `gate`, `include_rejected=false`.

### `GET /api/v1/analyses/{run_id}/initiatives/{initiative_id}` → `InitiativeDetail`
Всё для экрана 5 (FR-070): инициатива, процесс и операции с цитатами, проблемы, экономика по
трём сценариям (`economics` — автономный эффект, `attributed` — вклад в портфель,
`in_portfolio`, `excluded_reason`), использованные допущения (ключи и значения), разбивка
приоритета (`priority`: критерии, балл, ранг, группа, правило и причина), статус
допустимости, связи, история решений по элементу, `version` инициатив процесса и
`previous_decisions` — решения по инициативам и проблемам прежних версий процесса
`{event, version, title}` (FR-083).

### `GET /api/v1/analyses/{run_id}/economics` → `EconomicsResult`

### `GET /api/v1/analyses/{run_id}/assumptions` → `AssumptionSet` (текущая версия)
Заголовок `ETag: "<version>"`. Версия 1 создаётся в S7 по профилю (T043); ключи элементов,
появившихся позже, дозаполняются по профилю при чтении новой версией.

### `PATCH /api/v1/analyses/{run_id}/assumptions` (заголовок `If-Match`)
Тело: `{changes: list[{key, values?: Triple, note?}], comment?}`. Создаётся новая версия, в
журнал пишется `ReviewEvent(entity_type="assumption_set", entity_id="assumptions",
action="edit")` с `patch` и `previous` по ключам. Изменённое значение получает источник
`user` и снимает `requires_input`; правка только `note` источник не меняет. Новый ключ можно
добавить только один: `proc.<process_id>.hourly_cost_override` существующего процесса. Ответ —
`200 {assumption_set, initiatives: InitiativeTable, warnings}` (заголовок `ETag` новой
версии): сразу пересчитанная таблица, ни одного вызова LLM (SC-005). Валидация FR-057:
`400 validation_error`, `details.keys` — ключ → причина (отрицательные значения, доля вне
[0; 1], горизонт, неизвестный или повторный ключ, расчёт невозможен — например, нулевой
фонд времени FTE в любом сценарии); при ошибке версия не создаётся. Нелогичный порядок
сценариев возвращается в `warnings`, а не ошибкой.

### `GET /api/v1/analyses/{run_id}/priority-config` → `PriorityConfig` (+ `ETag`)
Версия 1 создаётся при первом чтении из `config/priority_default.toml`.

### `PUT /api/v1/analyses/{run_id}/priority-config` (заголовок `If-Match`)
Тело: `PriorityConfig` (невалидное — `400 validation_error`). Ответ — `200 {config, version,
initiatives: InitiativeTable}` с `ETag` новой версии: рейтинг и портфель пересчитаны, ни одного
вызова LLM. Та же конфигурация — та же версия, без события. Изменение пишет
`ReviewEvent(entity_type="priority_config", entity_id="priority_config", action="edit")` с
изменёнными полями в `patch` и `previous`.

## Экспертная проверка (US6)

### `POST /api/v1/analyses/{run_id}/reviews`
Тело: `{entity_type, entity_id, action, patch?, comment?}`. `entity_type` — `process`,
`operation`, `decision_point`, `finding`, `initiative` или `relation` (допущения и веса
меняются своими версионируемыми маршрутами). Правила:
- для `reject` требуется `comment` (пустой или из пробелов — `400`);
- `patch` — только у `edit` и `set_gate` и только поля из белого списка типа сущности
  (data-model §8), иначе `400`;
- для `set_gate` поле `patch.status` должно быть `LegalGate`;
- цитата в поле с основанием — `{value, citation: {fragment_id?, quote}}` — проверяется по
  тексту акта, как цитаты модели (FR-025): `verified` и `relocated` (ссылка исправляется на
  фрагмент с цитатой) сохраняются с результатом проверки, иначе — `400`,
  `details.fields` — поле → причина; цитата из утратившего силу фрагмента не принимается;
  без `fragment_id` цитата ищется во всём акте, основанием становится наименьший содержащий
  её фрагмент (`verified`);
- снять `legally_significant` у операции можно только с `comment` (data-model §3), иначе `400`;
- `relation`: `entity_id` — два id инициатив через «|» в любом порядке, хранится по алфавиту;
- решение принимается, только если весь журнал с ним применим (переход статуса по диаграмме,
  допустимое значение), иначе `400`; решения одного запуска записываются по одному.

Неизвестный запуск или элемент — `404`. Ответ — `201 {event: ReviewEvent, entity:
<сущность после решения>, stale_initiative_ids: list[str]}`: для связи `entity` —
`{initiative_ids, relation}`; `stale_initiative_ids` — все инициативы запуска, требующие
пересмотра после решения. `event.previous` у `edit` и `set_gate` — прежние значения
изменённых полей (для `ratings` — только переоценённых критериев).

Решения применяются при каждом чтении карты процессов, инициатив, экономики, допущений и
экрана настроек (T051): отклонённое исключено из расчётов, исправленное считается с новым
значением, выход этапов не меняется (FR-081). В строке таблицы инициатив `excluded_reason` —
почему инициатива вне расчёта (тогда `economics` и `priority` — `null`), баннер — если есть
инициативы, требующие пересмотра.

### `GET /api/v1/analyses/{run_id}/reviews` → `list[ReviewEvent]`
Фильтры: `entity_type`, `entity_id`, `user`. Сортировка по `created_at` по убыванию. Журнал
полный: и решения по элементам, и правки допущений и весов.

## Экспорт (US7)

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/api/v1/analyses/{run_id}/export.json?scenario=` | `application/json`, `Content-Disposition: attachment` — `AnalysisExport` |
| GET | `/api/v1/analyses/{run_id}/export/initiatives.csv?scenario=` | CSV UTF-8 с BOM, разделитель `;` |
| GET | `/api/v1/analyses/{run_id}/export/operations.csv` | CSV |
| GET | `/api/v1/schema/analysis-export.json` | JSON Schema `AnalysisExport` (из Pydantic) |

Файлы приходят вложением (T055): `analysis-<run_id>-<scenario>.json`,
`initiatives-<run_id>-<scenario>.csv`, `operations-<run_id>.csv`; `scenario` по умолчанию
`base`, неизвестный — `400`; неизвестный запуск — `404`; JSON и CSV инициатив — после S7,
CSV операций — после S5. Схема — `application/schema+json`. Ни одного вызова LLM.

### `AnalysisExport` (schema_version `1.0`)
```text
{schema_version, exported_at, exported_by, scenario,
 document: Document, structure: list[Fragment],
 run: AnalysisRun (config_snapshot, usage),
 processes: list[Process], coverage, unverified,
 findings: list[Finding], initiatives: list[Initiative], no_change: list[NoChangeDecision],
 excluded: dict[initiative_id, причина],          # вне расчёта по решениям эксперта
 initiative_versions: dict[process_id, int],      # действующая версия инициатив (T052)
 assumptions: AssumptionSet, priority_config: PriorityConfig, priority_config_version,
 economics: {conservative, base, optimistic: EconomicsResult},
 priority: list[PriorityResult],
 reviews: list[ReviewEvent],                      # весь журнал в порядке решений
 disclaimers: list[str]}                          # баннеры таблицы сценария `scenario`
```
У каждого элемента есть `layer` и `review.status` (FR-093): процесс, операция и точка решения —
`interpretation` (поля с основанием — свой слой), проблема и инициатива — `hypothesis`;
`layer` есть также у допущений (`assumption`), экономики (`assumption`) и приоритета
(`hypothesis`). Всё — с решениями эксперта и действующими версиями инициатив, как на экранах;
выходы этапов не меняются. Схема генерируется из модели (`export/json_export.py`).

### `initiatives.csv` — колонки
`rank;group;gate;score;initiative_id;title;process;operations;problem_patterns;solution_category;
regulatory_mode;automation_level;hours_per_year_y1;fte_y1;money_equiv_y1;capex;opex_y1;net_y1;
cum_net_horizon;roi_horizon;payback_months;in_portfolio;review_status;scenario;assumptions_version`

Строки — строки таблицы `/initiatives` того же сценария с `include_rejected=true`, в её
порядке; экономика — автономный эффект. Для людей и таблиц (T054): значения — подписи экрана
(«Классическая автоматизация», «Отклонено», «да»/«нет»), а не коды; числа с десятичной
запятой, округление только здесь (ROUND_HALF_EVEN): деньги — до копеек, часы — до десятых,
FTE — до сотых, ROI (доля) — до десятитысячных, балл — до десятых; у инициативы вне расчёта
числа пустые. Списки в ячейке — через «, »; строка с CRLF. Текст, который таблица приняла бы
за формулу (начинается с `=`, `+`, `-`, `@`), записывается с апострофом.

### `operations.csv` — колонки
`process_id;process_name;operation_id;seq;name;performer;action;operation_type;deadline_raw;
legally_significant;fragment_labels;quotes;grounding;confidence;review_status`

Все операции карты процессов с решениями эксперта (отклонённые — со статусом); `seq` — с 1,
как на карте; основание — цитаты действия, несколько — через « | ».

## Mermaid-генерация (детерминированная)
`flowchart TD`. Узлы `op_<n>["<seq> · <name> — <performer>"]` (не «<seq>.»: Mermaid читает
такую подпись как Markdown-список; кавычки и спецсимволы
экранируются, длина ≤ 80). Ромбы — для точек решения. Пунктирные рёбра — для
`is_loop_back`. Отклонённые операции не выводятся. Генерация — чистая функция с
golden-тестом.
