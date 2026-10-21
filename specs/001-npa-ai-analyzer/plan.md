# Implementation Plan: AI-анализ НПА

**Branch**: `feat/001-npa-ai-analyzer` | **Date**: 2026-10-21 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-npa-ai-analyzer/spec.md`

> Этот документ — `architecture.md` из постановки. Соответствие артефактов:
> `requirements.md` → [spec.md](./spec.md); `architecture.md` → этот файл + [research.md](./research.md);
> `data-models.md` → [data-model.md](./data-model.md); `api-contracts.md` →
> [contracts/api.md](./contracts/api.md), [contracts/llm.md](./contracts/llm.md),
> [contracts/ui.md](./contracts/ui.md); `tasks.md` → [tasks.md](./tasks.md).

## Summary

Монолитный веб-сервис на FastAPI. Детерминированный разбор документа (текст → дерево фрагментов
со стабильными id) идёт до LLM. Дальше — двухпроходное извлечение процессов LLM: карта процессов
по оглавлению, затем детализация каждого процесса со связанными фрагментами. Каждый факт
проверяется цитатой детерминированно. LLM предлагает инициативы с качественными оценками,
экономика и Priority Score — чистые функции на `Decimal`, они вычисляются при чтении из модели,
решений эксперта и версии допущений. UI — Jinja2 + HTMX, хранение — SQLite и файловый том, фоновые
задачи — встроенный runner с возобновлением по этапам.

## Technical Context

**Language/Version**: Python 3.12 (uv)

**Primary Dependencies**: FastAPI, Pydantic v2, pydantic-settings, structlog, SQLAlchemy 2.0
Core + aiosqlite, Jinja2 + HTMX + Mermaid (vendored), openai SDK (OpenAI-совместимый endpoint),
pypdfium2, python-docx, charset-normalizer, selectolax, rapidfuzz; системный `tesseract-ocr` + `rus`
для OCR (обоснование — research R-01…R-21)

**Storage**: SQLite (WAL) на томе `/data`; файлы — `/data/files/<sha256>`; PostgreSQL — через `DATABASE_URL`

**Testing**: pytest + pytest-asyncio; httpx `TestClient`; `FakeLLM`; property-тесты экономики
(собственные генераторы на `random.Random(seed)`, без hypothesis); eval на живой LLM вручную

**Target Platform**: Linux-сервер с Docker; локально — macOS/Linux через `uv run`

**Project Type**: web-service (монолит: API + серверный UI)

**Performance Goals**: разбор документа до 100 стр. ≤ 30 с (без OCR), OCR ≤ 10 с/стр.; пересчёт экономики и рейтинга ≤ 2 с
(цель ≤ 200 мс на 100 инициатив); полный анализ документа на ~130 тыс. знаков ≤ 15 мин [Прогноз]

**Constraints**: 1 воркер uvicorn (встроенный runner); CPU-работа (разбор, рендер, конвертация,
проверка цитат) — в пуле процессов (NFR-014); ≤ 2 одновременных анализа; лимиты загрузки
20 МБ / 300 стр. / 1 млн знаков; без внешних сервисов, кроме LLM; без сырых текстов в логах

**Scale/Scope**: 1 организация, ≤ 10 пользователей, десятки документов, ≤ 30 процессов и ≤ 150
инициатив на анализ

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Принцип | Как выполняется | Статус |
|---|---|---|
| I. Evidence-Grounded Extraction | `GroundedStr` с `fragment_id` и `quote` в каждой LLM-схеме; модуль `grounding` (R-11); слой «Не подтверждено»; валидатор S4 | ✅ |
| II. Deterministic Numbers | LLM возвращает только `Level5`/`DurationBucket`; `analysis/economics.py`, `scoring.py` на `Decimal`; вычисление при чтении; тест «0 вызовов LLM при пересчёте» | ✅ |
| III. Layered Knowledge | поле `layer` у всех элементов; бейджи в UI; `layer` и `disclaimers` в экспорте | ✅ |
| IV. Documents Are Data | разграниченный блок, экранирование, без инструкций; `LLM_DATA_POLICY` по умолчанию `disabled` (R-09, R-12) | ✅ |
| V. Test-First, LLM-Free, Eval Gate | `FakeLLM` и сценарии; eval-харнесс `evals/`; golden-разметка вне runtime | ✅ |
| VI. Human Oversight | `review_events` только на добавление; наложение; `stale`; ограничение автономности; `allowed_with_hitl` | ✅ |
| VII. Simplicity & Universality | 1 сервис, без брокера; структура и промпты без привязки к документу; dev-корпус (R-21), отложенная выборка ≥ 3 НПА, T060 | ✅ |
| Technical Constraints | uv, mypy strict, pydantic-settings, structlog, timeouts и retries, Docker | ✅ |
| Workflow | задачи из `tasks.md`; `scripts/check.sh`; feature-ветка | ✅ |

Повторная проверка после Phase 1 (data-model и contracts): нарушений нет, Complexity Tracking
пуст.

## Architecture

### Контекст

```text
            ┌──────────────── Docker host ────────────────┐
 Browser ──▶│  app (FastAPI, 1 worker)                    │──▶ LLM endpoint (OpenAI-compatible)
  (Basic)   │   ├─ web UI (Jinja2+HTMX)  ├─ /api/v1       │     по LLM_DATA_POLICY
            │   ├─ services ─ pipeline ─ JobRunner        │
            │   └─ storage: SQLite + /data/files  ◀─ volume
            └──────────────────────────────────────────────┘
```

### Pipeline

Логический pipeline из постановки ложится на этапы так:

```text
Upload → [S1 Ingest] → [S2 Structure] ──(sync, при загрузке)──────────────────────────┐
                                                                                      ▼
Run → [S3 Discover*] → [S4 Detail*] → [S5 Validate] → [S6 Opportunities*] → [S7 Postprocess]
                                                                                      │
Read → overlay(reviews) → [S8 Economics solo] → [S9 Gate + Priority] → [S10 Portfolio] → Report ◀┘
* — LLM; остальные этапы детерминированы. S8–S10 — чистые функции при каждом чтении.
```

| Этап постановки | Компонент | Модуль |
|---|---|---|
| Document Parsing | S1 Ingest (+ OCR в фоне), очистка | `ingestion/`, `structure/cleanup.py` |
| Structure Extraction | S2 Structure | `structure/` |
| Process Extraction | S3 Discover + S4 Detail | `extraction/discovery.py`, `detailing.py` |
| Process Validation | S5 Validate | `validation/`, `grounding/` |
| Optimization Analysis + AI Opportunity Generation | S6 Opportunities | `extraction/opportunities.py` |
| Risk Assessment | S7 Postprocess (gating, tech guard, связи) + S9 | `analysis/gating.py`, `tech_guard.py`, `relations.py` |
| Economic Estimation | S8 (solo) + S10 (портфель, атрибуция) | `analysis/economics.py`, `portfolio.py` |
| Priority Ranking | S9 | `analysis/scoring.py`, `grouping.py` |
| Report | export, UI | `export/`, `web/` |

### Компоненты: ответственность и контракты

| Компонент | Ответственность | Вход | Выход | Контракт | Ошибки | Критерии качества |
|---|---|---|---|---|---|---|
| **S1 Ingest** | Определить формат, извлечь текст постранично, диагностика; страницы без текста распознаются OCR фоновой задачей | bytes, filename | `RawDocument{format, blocks: list[Block], diagnostics}` (IR, R-22) | `Extractor` Protocol: `extract(data) -> RawDocument`; реестр по `DocFormat` | `unsupported_format`, `file_too_large`, `corrupted_file`, `encrypted_file`, `no_text_layer`, `empty_document`, `encoding_undetected` | 100 % фикстур корректно классифицированы; скан → `needs_ocr`; без исключений наружу, кроме `IngestionError` |
| **S2 Structure** | Очистка (служебные строки, пометки, колонтитулы), профиль нумерации, дерево фрагментов, метки, смещения, страницы, реквизиты | `RawDocument` | `list[Fragment]`, `DocumentMetadata`, `StructureMode` | `build_structure(raw, strategies) -> StructureResult`; стратегии соревнуются по оценке (R-22); чистая функция | не бросает исключений: деградирует в режим `paragraphs` с предупреждением | инварианты вложенности; снимки структуры dev-корпуса совпадают (SC-012); на регламенте 261 разделы I–V и пункты 3.x.y распознаны |
| **S3 Discover** | Карта процессов по оглавлению | fragments → outline | `DiscoveryOut` → кандидаты | [llm.md § S3](./contracts/llm.md#s3--discover--карта-процессов) | LLM-коды; 0 валидных кандидатов → этап `succeeded` с «процессы не найдены» | полнота и точность процессов (eval) |
| **S4 Detail** | Операции, решения, роли, сроки для каждого процесса | кандидат + фрагменты | `DetailOut` по каждому процессу | [llm.md § S4](./contracts/llm.md#s4--detail--декомпозиция-процесса-до-операций) | ошибка одного процесса → процесс помечается `failed`, этап `succeeded` с предупреждением и `completeness.complete=false`, если доля ошибок ≤ 30 %, иначе `failed` | полнота операций, доля `verified` (eval) |
| **S5 Validate** | Grounding, граф, дедупликация, уверенность, покрытие | `DetailOut[]`, fragments | `ProcessModel{processes, unverified, coverage}` | `validate(detail, fragments, cfg) -> ProcessModel`; чистая | не бросает | 100 % «фактов» с verified/relocated-цитатой (SC-002) |
| **S6 Opportunities** | Проблемы и инициативы с enum-оценками | эффективная модель процесса, каталог | `OpportunitiesOut` по каждому процессу | [llm.md § S6](./contracts/llm.md#s6--opportunities--проблемы-и-инициативы) | как S4 | метрики S6 (eval, judge) |
| **S7 Postprocess** | Ссылочная целостность, tech guard, ограничение автономности, статус допустимости, связи, допущения по умолчанию | `OpportunitiesOut[]`, модель, профиль | `InitiativeSet`, `AssumptionSet v1` | чистые функции `analysis/*` | не бросает; issues в отчёт | инварианты data-model §4, §6 |
| **S8 Economics (solo)** | Формулы спеки для автономного эффекта, сценарии | эффективные модель и инициативы, `AssumptionSet`, сценарий | `EconomicsResult` (`layer=assumption`) | `compute_solo(...) -> EconomicsResult`; чистая, `Decimal` | `AssumptionValidationError` → 400 | ручные примеры; независимость от порядка; идентичность повторов |
| **S9 Priority** | Нормирование, балл, группы, ранжирование | инициативы, `EconomicsResult(base)`, `PriorityConfig` | `list[PriorityResult]` | `rank(...)`; чистая | — | ручной пересчёт по разбивке совпадает; стабильность сортировки |
| **S10 Portfolio** | Состав портфеля, мультипликативное комбинирование, атрибуция (FR-054, FR-055) | solo-экономика, `PriorityResult[]`, связи | `PortfolioEconomics` | `build_portfolio(...)`; чистая | — | property-тесты (data-model §7): `Σ attributed = portfolio`, `combined ≤ 1` |
| **Overlay** | Наложение решений эксперта | вывод этапов, `ReviewEvent[]` | эффективная модель + `stale` | `apply_reviews(...)`; чистая | `400` на патч вне белого списка | идемпотентность, порядок по `created_at` |
| **JobRunner** | Очередь, параллелизм, статусы, возобновление | `run_id` | обновления `AnalysisRun`, `stage_outputs` | `submit(run_id)`, `resume(run_id)`, `cancel(run_id)` | исключение этапа → `failed` + код; старт → `interrupted` | тест возобновления после «краха» |
| **LLM client** | Структурированные вызовы, retries, кэш, учёт, политика | schema, prompts | `LLMResult[T]` | [llm.md § Общий протокол](./contracts/llm.md#общий-протокол) | коды [llm.md](./contracts/llm.md) | 0 сетевых вызовов в тестах; логи без текстов |
| **API / Web** | HTTP, аутентификация, рендеринг | HTTP | JSON / HTML | [api.md](./contracts/api.md), [ui.md](./contracts/ui.md) | формат ошибки api.md | contract-тесты по OpenAPI |
| **Export** | JSON по схеме, CSV | эффективное состояние | файлы | api.md § Экспорт | — | JSON валиден по схеме; CSV = экрану |

### Хранение промежуточных результатов

- Каждый этап S3–S7 пишет в `stage_outputs` свой валидированный вывод одним документом JSON
  после завершения. S4 и S6 пишут по каждому процессу в `payload.items[process_key]`, чтобы
  возобновление не повторяло готовые процессы.
- Перегенерация инициатив процесса (FR-083) создаёт запись S6 с суффиксом версии
  (`opportunities@2`). Эффективной считается последняя. Решения по прежним инициативам
  остаются в журнале и показываются как «по предыдущей версии». Пользовательские допущения
  переносятся по совпадению операций и категории (data-model §8).
- Сырые ответы LLM хранятся в `llm_calls.response` для каждого вызова, независимо от кэша
  (FR-030).
- Экономика и приоритет не хранятся. Аудит обеспечивают версии допущений и конфигурации плюс
  чистые функции.

### Конфигурация (env, pydantic-settings; `.env.example`)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `APP_ENV` | `local` | `local` / `server` |
| `APP_PORT` | `8000` | |
| `DATA_DIR` | `/data` (`./data` локально) | БД и файлы |
| `DATABASE_URL` | `sqlite+aiosqlite:///${DATA_DIR}/gr.db` | |
| `AUTH_ENABLED` | `true` | `false` только при `APP_ENV=local` |
| `AUTH_USERS` | — | `login:scrypt$salt$hash,…` |
| `MAX_UPLOAD_MB` / `MAX_PAGES` / `MAX_CHARS` | 20 / 300 / 1 000 000 | FR-002 |
| `MAX_UNZIPPED_MB` | 100 | лимит распаковки DOCX (R-04) |
| `APP_PUBLIC_URL` | — | внешний адрес за реверс-прокси (проверка Origin, R-15); обязателен при `APP_ENV=server` |
| `PARSE_WORKERS` / `PARSE_TIMEOUT_S` | 2 / 120 | пул процессов (NFR-014) |
| `STRUCTURE_MIN_CONFIDENCE` | 0.5 | ниже — предупреждение о структуре (R-22) |
| `SQLITE_BUSY_TIMEOUT_MS` | 5000 | |
| `LLM_MAX_OUTPUT_TOKENS` | 8000 | подбирается под модель (T074) |
| `OCR_ENABLED` | `true` | OCR сканов (FR-008) |
| `OCR_LANG` / `OCR_DPI` | `rus+eng` / 300 | |
| `OCR_MAX_PAGES` / `OCR_PAGE_TIMEOUT_S` / `OCR_MIN_CONFIDENCE` / `OCR_CONCURRENCY` | 150 / 60 / 60 / 1 | |
| `LLM_DATA_POLICY` | `disabled` | `disabled` / `local_only` / `external_allowed` |
| `LLM_BASE_URL`, `LLM_API_KEY` | — | OpenAI-совместимый endpoint |
| `LLM_LOCAL_HOSTS` | `localhost,127.0.0.1,ollama,vllm` | для `local_only` |
| `LLM_MODEL_EXTRACTION`, `LLM_MODEL_ANALYSIS` | — (обязательны при политике ≠ `disabled`) | |
| `LLM_STRUCTURED_MODE` | `auto` | `json_schema` / `json_object` / `auto` |
| `LLM_TIMEOUT_S` / `LLM_MAX_RETRIES` / `LLM_CONCURRENCY` | 120 / 2 / 4 | |
| `LLM_CACHE_ENABLED` | `true` | |
| `LLM_PRICE_INPUT_PER_MTOK`, `LLM_PRICE_OUTPUT_PER_MTOK` | — | оценка стоимости (NFR-011) |
| `MAX_CONCURRENT_ANALYSES` | `2` | |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `console`/`json` | |

Пороги проверки цитат, лимиты чанкинга, профиль допущений, каталог проблем и умолчания
приоритизации лежат в `src/gr/config/*.toml` (данные пакета, версионируются в git, хэш —
в `RunConfig`).

### Docker

- Dockerfile, `compose.prod.yml`, CI доставки и runbook генерируются skill'ом `/deploy-kit` (R-16):
  uv-образ, non-root, `HEALTHCHECK` по `/healthz`. В образ ставятся `tesseract-ocr` и
  `tesseract-ocr-rus`, а для конвертации — `libreoffice-writer-nogui`. Права на том выставляются
  для пользователя приложения, `HEALTHCHECK` идёт через `python -c`, `OMP_THREAD_LIMIT=1`.
- Реверс-прокси: `client_max_body_size` ≥ `MAX_UPLOAD_MB`, `proxy_read_timeout` 120 с,
  `X-Forwarded-*`, `APP_PUBLIC_URL`.
- `docker-compose.yml` для AC-10: сервис `app` (build `.`, порт `${APP_PORT}`, том `gr-data:/data`,
  `env_file: .env`, `restart: unless-stopped`) и опциональный профиль `local-llm` (`ollama`) для
  `local_only`.
- Требования к серверу `[Прогноз]`: 4 vCPU (OCR), 4 ГБ RAM, 20 ГБ диска для приложения без локальной
  LLM. С локальной LLM — GPU или значительно больше RAM, зависит от модели.

## Project Structure

### Documentation (this feature)

```text
specs/001-npa-ai-analyzer/
├── spec.md              # requirements (Phase 1)
├── plan.md              # architecture (этот файл)
├── research.md          # решения и альтернативы
├── data-model.md        # модели данных
├── quickstart.md        # сценарии проверки
├── contracts/
│   ├── api.md           # REST + экспорт
│   ├── llm.md           # контракты LLM-этапов
│   └── ui.md            # экраны
├── checklists/requirements.md
└── tasks.md             # Phase 3 (/speckit-tasks)
```

### Source Code (repository root)

```text
src/gr/
├── __init__.py, __main__.py      # CLI: serve, hash-password
├── settings.py                   # pydantic-settings
├── logging.py                    # structlog
├── app.py                        # create_app(): API + web + lifespan (db, runner)
├── auth.py, errors.py
├── domain/                       # Pydantic-модели и enums (data-model.md)
│   ├── enums.py, document.py, process.py, initiative.py
│   ├── assumptions.py, priority.py, review.py, run.py
├── ingestion/                    # S1
│   ├── base.py, detect.py, registry.py
│   ├── ir.py, pdf.py, pdf_layout.py, docx.py, docx_numbering.py, txt.py, html.py, ocr.py,
│   │   convert.py
├── structure/                    # S2
│   ├── cleanup.py, patterns.py, profiles.py, strategies.py, scoring.py, builder.py, metadata.py
├── grounding/                    # R-11
│   ├── normalize.py, quotes.py, numerals_ru.py, deadlines.py, roles.py
├── llm/
│   ├── base.py (StructuredLLM, LLMResult, ошибки), openai_compat.py
│   ├── fake.py, cache.py, policy.py
├── extraction/                   # S3, S4, S6
│   ├── schemas.py, outline.py, render.py (блок <document>)
│   ├── discovery.py, detailing.py, opportunities.py, mapping.py
│   └── prompts/ discover.md, detail.md, opportunities.md
├── validation/                   # S5
│   ├── process_validator.py, graph.py, dedupe.py, confidence.py, coverage.py
├── analysis/                     # S7–S9 + overlay
│   ├── postprocess.py, tech_guard.py, gating.py, relations.py, defaults.py
│   ├── economics.py, scoring.py, grouping.py, portfolio.py, overlay.py, mermaid.py
├── pipeline/
│   ├── stages.py, runner.py, workers.py (пул процессов)
├── storage/
│   ├── db.py, repositories.py, files.py
├── services/                     # общий для API и web сервисный слой
│   ├── documents.py, analyses.py, results.py, reviews.py
├── api/
│   ├── deps.py, documents.py, analyses.py, results.py, reviews.py, exports.py, health.py
├── web/
│   ├── routes.py
│   ├── templates/ base.html, documents.html, document.html, run.html, processes.html,
│   │              initiatives.html, initiative.html, settings.html, history.html, partials/*
│   └── static/ app.css, app.js, vendor/{htmx.min.js, mermaid.min.js, VERSIONS}
├── export/
│   ├── json_export.py, csv_export.py
└── config/
    ├── structure_patterns.toml, noise_patterns.toml, grounding.toml, chunking.toml
    ├── assumption_profile.toml, problem_catalog.toml, priority_default.toml

tests/
├── unit/          # grounding, structure, economics, scoring, gating, overlay, mermaid, ingestion
├── integration/   # pipeline на FakeLLM, runner, API + web через TestClient
├── contract/      # OpenAPI ↔ contracts/api.md, JSON Schema экспорта
├── property/      # инварианты разбора и fuzz на любом входе (SC-014)
└── fixtures/
    ├── make_fixtures.py, fonts/DejaVuSans.ttf
    ├── synthetic/  # мини-регламент (txt/docx/pdf), скан, битый файл, инъекция, без нумерации
    ├── npa/        # регламент 261 (docx/pdf) + SOURCES.md
    ├── corpus/     # dev-корпус НПА разных типов + снимки структуры (R-21)
    └── llm/        # сценарии FakeLLM (JSON)

evals/             # формат /eval: dataset.jsonl, target.py, config.toml, baseline.json,
                   # golden/*.json (3 документа), markers.toml, holdout/SELECTED.md
docker-compose.yml, Dockerfile (deploy-kit), .env.example, README.md, docs/deploy.md
```

**Structure Decision**: один пакет `src/gr` (src-layout от `uv init`). Слои: `domain` (модели) ←
`grounding`/`structure`/`analysis` (чистые функции) ← `extraction`/`pipeline` (LLM и
оркестрация) ← `services` ← `api`/`web`. Зависимости только сверху вниз, это проверяется
тестом импортов: `domain`, `analysis`, `grounding` не импортируют `llm`, `storage`, `fastapi`.

## Validation Plan (Phase 5)

1. Eval на регламенте 261 по golden-разметке, метрики SC-002…SC-004 и R-17.
2. Отложенная выборка (≥ 3 НПА ≥ 2 типов, включая скан): полный сквозной сценарий без
   изменения кода (SC-007). Ручной аудит фактов (SC-004). Метрики по каждому документу — в
   отчёте.
3. Проверка воспроизводимости экономики и рейтинга (SC-006) и пересчёта без LLM (SC-005).
4. Сценарии отказа (SC-008) и инъекция (SC-011).
5. Итог — `docs/validation-report.md`: найденные ошибки, ограничения MVP, предложения.
   Пример результата анализа регламента — `docs/examples/reg261/` (экспорт JSON и CSV и
   скриншоты экранов).

## Open Questions

Не блокируют реализацию: у каждого вопроса есть решение по умолчанию.

1. **LLM-провайдер и модель для eval и демо.** По умолчанию — OpenRouter (OpenAI-совместимый)
   с моделью класса Sonnet и `LLM_DATA_POLICY=external_allowed` для публичных НПА. **Решено
   2026-10-21: OpenRouter** (подробности — R-08, «Провайдер»). Адаптер `AnthropicLLM` не нужен.
   Спайк T074 и eval (T036, T059) ждут ключа в окружении.
2. **Экономические умолчания** (ставка 1 200 ₽/ч, накладные 30 %, фонд 1 970 ч/год, 1 000
   случаев/год) — плейсхолдеры. Если есть данные организации, они пойдут в профиль.
3. **Состав dev-корпуса и отложенной выборки** (R-21). Подбираю сам по критериям R-21. Если у
   тестировщиков есть свои типичные документы или источники, лучше добавить их в dev-корпус
   (не в отложенную выборку).
4. **Сервер**: ОС, домен, TLS (Caddy или nginx), доступ — нужны к Phase 6.

## Complexity Tracking

Нарушений конституции нет.
