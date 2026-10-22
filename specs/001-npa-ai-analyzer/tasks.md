---
description: "Task list for 001-npa-ai-analyzer"
---

# Tasks: AI-анализ НПА — процессы, возможности автоматизации, экономика, приоритизация

**Input**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md),
[data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Tests**: обязательны (конституция, принцип V). Тесты задачи пишутся первыми и должны падать
до реализации.

**Organization**: по user stories из spec.md. Приоритет P1 (US1, US2) = MVP-срез.

## Format

`- [ ] T### [P?] [US?] Название — основной файл`, под ним:
**Цель** · **Зависит** · **Файлы** · **Результат** · **Готово, когда** · **Тесты**.

- **[P]** — можно выполнять параллельно с другими [P] той же фазы (разные файлы, нет зависимостей).
- **[USn]** — user story из spec.md. **[manual]** — нужен человек (данные, разметка, доступ).
- Общий критерий для каждой задачи: `./scripts/check.sh` зелёный.

---

## Phase 1: Setup

- [X] T001 Зависимости и конфигурация инструментов — `pyproject.toml`
  - **Цель**: подключить стек из research R-18.
  - **Зависит**: —
  - **Файлы**: `pyproject.toml`, `uv.lock`
  - **Результат**: `uv add` runtime-зависимостей (fastapi, uvicorn[standard], python-multipart,
    jinja2, pydantic-settings, structlog, sqlalchemy[asyncio], aiosqlite, openai, pypdfium2,
    python-docx, charset-normalizer, selectolax, rapidfuzz); `uv add --dev httpx fpdf2`; плагин
    `pydantic.mypy`.
  - **Готово, когда**: `uv sync` и `check.sh` проходят, версии зафиксированы в `uv.lock`.
  - **Тесты**: существующий smoke-тест.

- [X] T002 [P] Каркас пакета и тест слоёв — `tests/unit/test_layering.py`
  - **Цель**: зафиксировать структуру из plan.md и запрет обратных зависимостей.
  - **Зависит**: T001
  - **Файлы**: `src/gr/**/__init__.py` (все пакеты плана), `tests/unit/test_layering.py`
    и `tests/unit/test_spec_consistency.py` (уже добавлен)
  - **Результат**: пустые пакеты. Тест проверяет AST-импорты: `domain`, `grounding`, `analysis`,
    `structure` не импортируют `llm`, `storage`, `fastapi`, `openai`, `sqlalchemy`.
    Плюс тест согласованности артефактов: зависимости задач существуют и без циклов, каждое FR/NFR/SC упомянуто вне spec.md.
  - **Готово, когда**: тест зелёный, а намеренное нарушение его роняет (проверено вручную).
  - **Тесты**: `test_layering.py`.

- [X] T003 [P] Настройки и `.env.example` — `src/gr/settings.py`
  - **Цель**: вся конфигурация через env (plan § Конфигурация).
  - **Зависит**: T001
  - **Файлы**: `src/gr/settings.py`, `.env.example`, `tests/unit/test_settings.py`
  - **Результат**: `Settings(BaseSettings)` с валидацией. `AUTH_ENABLED=false` разрешён только
    при `APP_ENV=local`. При `LLM_DATA_POLICY≠disabled` обязательны `LLM_BASE_URL` и
    `LLM_MODEL_*`. Лимиты > 0.
    При `APP_ENV=server` обязателен `APP_PUBLIC_URL`, иначе сервис не стартует (NFR-006: ключи только из env).
  - **Готово, когда**: каждое правило валидации покрыто тестом; `.env.example` перечисляет все
    переменные без значений секретов.
  - **Тесты**: `test_settings.py`.

- [X] T004 [P] Логирование — `src/gr/logging.py`
  - **Цель**: structlog с контекстом `run_id`, `doc_id`, `stage` (NFR-002).
  - **Зависит**: T001
  - **Файлы**: `src/gr/logging.py`, `tests/unit/test_logging.py`
  - **Результат**: `configure_logging(level, fmt)`, `bind_context(...)`, `text_fingerprint(s) ->
    {len, sha256_8}` для логирования текстов без содержимого.
  - **Готово, когда**: JSON-формат валиден; в событиях нет полей длиннее 200 символов (тест).
  - **Тесты**: `test_logging.py`.

- [X] T005 [P] Генератор синтетических фикстур — `tests/fixtures/make_fixtures.py`
  - **Цель**: воспроизводимые тестовые документы без привязки к реальному НПА.
  - **Зависит**: T001
  - **Файлы**: `tests/fixtures/make_fixtures.py`, системный TTF с кириллицей (DejaVu или Arial; в репо не кладётся),
    `tests/fixtures/synthetic/*`
  - **Результат**: вымышленный «Регламент выдачи пропусков» (3 раздела, 2 процесса, сроки,
    перечень оснований отказа) в форматах: `txt` (UTF-8 и cp1251), `docx` с ручной нумерацией,
    `docx` с автонумерацией, `pdf` с текстом, `pdf`-скан (изображение), битый `pdf`, `html`,
    `injection.txt` (внедрённые инструкции), `no_numbering.txt`, `law_like.txt` (статьи и
    части), `multi_approved.docx` (приказ с двумя утверждаемыми документами),
    `spps_export.docx` (служебные строки, пометки «в ред.», утративший силу пункт), `headers.pdf`
    (колонтитулы, пункт через границу страницы), `table.docx` (сроки в таблице), `scan.pdf`
    300 dpi с известным текстом (для CER). Сгенерированные файлы
    коммитятся.
  - **Готово, когда**: повторный запуск даёт тот же текстовый контент; файлы открываются.
  - **Тесты**: `tests/unit/test_fixtures_present.py` (наличие и базовые свойства).

- [X] T006 [P] [manual] Реальные фикстуры и отложенная выборка — `tests/fixtures/npa/SOURCES.md`
  - **Цель**: зафиксировать тестовый регламент и отложенную выборку до начала настройки промптов (R-17, R-21).
  - **Зависит**: —
  - **Файлы**: `tests/fixtures/npa/reg261.docx`, `reg261.pdf`, `SOURCES.md`;
    `evals/holdout/SELECTED.md`
  - **Результат**: файлы регламента 261 (ред. 30.05.2023) с источником, датой получения и
    sha256. Отмечено, проверялись ли изменения после 30.05.2023. В `SELECTED.md` — реквизиты
    и sha256 отложенной выборки (≥ 3 НПА ≥ 2 типов, включая скан; R-21). Файлы кладутся в
    `evals/holdout/` (в `.gitignore`) и не открываются до T061.
  - **Готово, когда**: sha256 в `SOURCES.md` совпадают с файлами; holdout выбран и записан.
  - **Тесты**: —

- [ ] T071 [P] [manual] Dev-корпус НПА разных типов — `tests/fixtures/corpus/SOURCES.md`
  - **Цель**: проверка универсальности разбора без LLM (R-21, SC-012).
  - **Зависит**: T001
  - **Файлы**: `tests/fixtures/corpus/*` (документы, `SOURCES.md`),
    `tests/fixtures/corpus/*.structure.json` (снимки, создаются в T068), `evals/markers.toml`
  - **Результат**: ≥ 6 НПА по матрице R-21 (типы, уровни власти, источники, форматы, один скан)
    с реквизитами, источником, датой получения и sha256. В `markers.toml` — маркеры документов
    eval-набора и отложенной выборки. С отложенной выборкой корпус не пересекается.
  - **Готово, когда**: матрица R-21 покрыта; sha256 совпадают с файлами.
  - **Тесты**: регрессия в T068.

- [X] T060 [P] Проверка универсальности кода (с первого дня) — `tests/unit/test_no_document_specific_code.py`
  - **Цель**: NFR-001, принцип VII.
  - **Зависит**: T002
  - **Файлы**: `tests/unit/test_no_document_specific_code.py`
  - **Результат**: тест ищет в `src/`, `config/` и `extraction/prompts/` маркеры документов из
    `evals/markers.toml` (eval-набор и отложенная выборка; для регламента 261 — «Росимущ»,
    «земельн», «59820»).
  - **Готово, когда**: тест зелёный.
  - **Тесты**: сам тест.

---

## Phase 1b: Спайки (снятие рисков до основного кода)

**Purpose**: проверить на реальных данных допущения, от которых зависит работоспособность MVP
(pre-mortem). Итоги записываются в `research.md`, а затем закрепляются тестами в задачах.

- [X] T072 [manual] Спайк: разбор реальных документов — `spikes/parsing/`
  - **Цель**: снять риски разбора до основного кода: сборка абзацев PDF, повторы нумерации,
    потери текста DOCX (FR-010, FR-011).
  - **Зависит**: T001, T006
  - **Файлы**: `spikes/parsing/` (одноразовые скрипты, не входят в пакет), `research.md`
    (итоги в R-04…R-06)
  - **Результат**: на reg261 (PDF и DOCX) и двух документах dev-корпуса другого типа выяснено:
    как pypdfium2 отдаёт строки и переносы; сколько ложных «пунктов» даёт наивный разбор;
    совпадает ли `paragraph.text` с обходом XML; встречаются ли повторы номеров.
  - **Готово, когда**: по каждому риску в research.md записан вывод «подтвердился / нет» и
    выбранное решение; при необходимости уточнены T016, T017, T020.
  - **Тесты**: — (выводы закрепляются тестами в T016, T017, T020).

- [X] T073 Спайк: образ с tesseract и LibreOffice — `spikes/docker/Dockerfile`
  - **Цель**: размер образа, скорость OCR и конвертации, права на том, healthcheck без curl
    (FR-008, NFR-014).
  - **Зависит**: T001
  - **Файлы**: `spikes/docker/Dockerfile`, `research.md` (R-04, R-16)
  - **Результат**: замеры — размер образа, секунд на страницу OCR (300 dpi, rus+eng), время
    конвертации `.doc` → `.docx`; проверено, что non-root пользователь пишет в именованный том;
    healthcheck через `python -c` работает.
  - **Готово, когда**: цифры записаны в research.md; если OCR > 10 с/стр., скорректированы
    SC-001 или параметры (`OCR_DPI`, `tessdata_fast`).
  - **Тесты**: —.

- [X] T074 [manual] Спайк: структурированный вывод у выбранного провайдера — `spikes/llm/`
  - **Цель**: подтвердить контракт LLM на реальном провайдере (NFR-003). Провайдер — OpenRouter.
    Итоги — research.md, R-08, «Итоги спайка T074».
  - **Зависит**: T001
  - **Файлы**: `spikes/llm/`, `research.md` (R-08)
  - **Результат**: на одном реальном процессе reg261 со схемой `DetailOut` выяснено:
    поддерживается ли `json_schema` (strict) и с какими лимитами схемы; размер ответа в токенах;
    частота обрезания; задержка; стоимость; приходит ли `usage`; насколько точно копируются
    алиасы и цитаты. Если выбран Anthropic напрямую — решение об адаптере `AnthropicLLM`
    (tool use) и отдельная задача на него.
  - **Готово, когда**: значения по умолчанию `LLM_STRUCTURED_MODE` и `LLM_MAX_OUTPUT_TOKENS`
    подтверждены цифрами в research.md.
  - **Тесты**: —.

---

## Phase 2: Foundational (блокирует все user stories)

- [X] T007 Доменные модели: enums, документ, запуск, ревью — `src/gr/domain/`
  - **Цель**: модели data-model §1, §2, §8, §9.
  - **Зависит**: T002
  - **Файлы**: `domain/enums.py`, `document.py`, `run.py`, `review.py`,
    `tests/unit/domain/test_document_models.py`
  - **Результат**: frozen-модели с `extra="forbid"`; валидаторы `Fragment.start < end`, переходы
    `RunStatus`, обязательный `comment` у `reject`.
  - **Готово, когда**: каждое правило валидации из data-model для этих сущностей покрыто тестом.
  - **Тесты**: `test_document_models.py`.

- [X] T008 [P] Доменные модели: процессы, инициативы, допущения, приоритет — `src/gr/domain/`
  - **Цель**: модели data-model §3–§6.
  - **Зависит**: T007
  - **Файлы**: `domain/process.py`, `initiative.py`, `assumptions.py`, `priority.py`,
    `tests/unit/domain/test_analysis_models.py`
  - **Результат**: `Grounded[T]`, `Process`, `Operation`, `Deadline`, `DecisionPoint`,
    `Initiative`, `Triple`, `Assumption`, `AssumptionSet`, `PriorityConfig`. Инварианты:
    `requires_change ⇒ requires_legal_review`; доли в [0;1]; `fact` ⇒ ≥ 1 цитата; ≥ 2 исхода у
    точки решения.
  - **Готово, когда**: инварианты покрыты тестами.
  - **Тесты**: `test_analysis_models.py`.

- [X] T009 Хранилище — `src/gr/storage/`
  - **Цель**: таблицы data-model §10, репозитории, файловое хранилище.
  - **Зависит**: T007, T003
  - **Файлы**: `storage/db.py`, `repositories.py`, `files.py`, `tests/integration/test_storage.py`
  - **Результат**: async-engine, `create_all` + проверка версии схемы в `meta`, WAL. Репозитории
    по каждой таблице. Таблицы `review_events`, `assumption_sets`, `priority_configs` — только
    INSERT (в API репозитория нет update и delete). Файлы пишутся атомарно (tmp + rename) по
    sha256.
    Запись идёт через одно соединение-писатель под `asyncio.Lock`, `PRAGMA busy_timeout` (`SQLITE_BUSY_TIMEOUT_MS`).
  - **Готово, когда**: CRUD по каждой таблице проходит на временной SQLite; повторное
    открытие БД сохраняет данные.
    50 конкурентных записей runner, API и OCR проходят без `database is locked`.
  - **Тесты**: `test_storage.py`.

- [X] T010 [P] Ошибки, аутентификация, CLI — `src/gr/errors.py`, `src/gr/auth.py`
  - **Цель**: единый формат ошибок api.md и HTTP Basic (R-15).
  - **Зависит**: T003
  - **Файлы**: `errors.py`, `auth.py`, `__main__.py`, `tests/unit/test_errors.py`,
    `tests/unit/test_auth.py`
  - **Результат**: `AppError(code, status, message_ru, details)`, таблица кодов из api.md,
    обработчики FastAPI. Basic-зависимость со scrypt и `compare_digest`. CLI
    `gr hash-password`, `gr serve`. Middleware `same_origin_guard` (R-15).
  - **Готово, когда**: каждый код из таблицы api.md сериализуется в нужный HTTP-статус; неверный
    пароль → 401; хэш из CLI проходит проверку; POST с чужим `Origin` или
    `Sec-Fetch-Site: cross-site` → 403 `csrf_rejected`; тот же источник и запрос без этих
    заголовков проходят.
  - **Тесты**: `test_errors.py`, `test_auth.py`, `test_csrf.py`.

- [X] T011 Приложение, health, базовый шаблон, vendored-статика — `src/gr/app.py`
  - **Цель**: работающий каркас веб-сервиса.
  - **Зависит**: T009, T010, T004
  - **Файлы**: `app.py`, `api/health.py`, `api/deps.py`, `web/routes.py`, `web/templates/base.html`,
    `web/static/app.css`, `web/static/vendor/{htmx.min.js, mermaid.min.js, VERSIONS}`,
    `tests/integration/test_app.py`
  - **Результат**: `create_app()` с lifespan (БД, runner-заглушка), `/healthz` без
    аутентификации, `/api/v1/readyz` с ней. Базовый HTML с навигацией.
    `gr serve` запускает uvicorn с `--proxy-headers`; `FORWARDED_ALLOW_IPS` uvicorn читает из
    окружения, адрес привязки — флаг `gr serve --host` (в контейнере `0.0.0.0`).
  - **Готово, когда**: `uv run gr serve` поднимает сервис; `/healthz` → 200 без аутентификации,
    `/` без аутентификации → 401.
  - **Тесты**: `test_app.py`.

- [X] T012 [P] LLM: протокол, FakeLLM, политика данных — `src/gr/llm/`
  - **Цель**: контракт [llm.md § Общий протокол](./contracts/llm.md#общий-протокол) и NFR-009.
  - **Зависит**: T003
  - **Файлы**: `llm/base.py`, `fake.py`, `policy.py`, `tests/unit/llm/test_policy.py`,
    `tests/unit/llm/test_fake.py`
  - **Результат**: `StructuredLLM`, `LLMResult`, иерархия ошибок с кодами из llm.md. `FakeLLM`
    по сценарию со счётчиком вызовов. `check_data_policy(settings)`: loopback, частные IP,
    allowlist, внешние хосты.
  - **Готово, когда**: таблица политик (`disabled`, `local_only` + внешний хост →
    `external_llm_not_allowed`, `external_allowed`) покрыта тестами.
  - **Тесты**: `test_policy.py`, `test_fake.py`.

- [X] T013 LLM: OpenAI-совместимый клиент и кэш — `src/gr/llm/openai_compat.py`
  - **Цель**: структурированные вызовы с retries, repair, учётом токенов (R-08).
  - **Зависит**: T012, T009
  - **Файлы**: `llm/openai_compat.py`, `llm/cache.py`, `tests/unit/llm/test_openai_compat.py`
  - **Результат**: режимы `json_schema`, `json_object`, `auto`; один repair-повтор при
    `ValidationError`; распознавание `finish_reason=length`; запись в `llm_calls`; событие лога
    `llm.call`; сырой ответ в `llm_calls.response` при каждом вызове (FR-030); `CachedLLM` по
    `request_hash`. Перед реализацией сверить актуальный API
    structured outputs в `openai` через context7.
    Если `usage` не пришёл, токены оцениваются по длине с флагом `estimated`; `max_tokens` берётся из `LLM_MAX_OUTPUT_TOKENS`.
  - **Готово, когда**: на `httpx.MockTransport` (без сети) проверены успех, переход 400 → откат
    на `json_object`, невалидный ответ → repair → успех и → `llm_invalid_output`, таймаут →
    `llm_timeout`, 401 → `llm_auth_failed`, обрезанный ответ. В логах нет текстов.
  - **Тесты**: `test_openai_compat.py`, `test_cache.py`.

- [X] T014 Фоновый runner — `src/gr/pipeline/runner.py`
  - **Цель**: очередь, параллелизм, сохранение по этапам, возобновление (R-03).
  - **Зависит**: T009
  - **Файлы**: `pipeline/runner.py`, `pipeline/stages.py` (протокол `Stage`),
    `tests/integration/test_runner.py`
  - **Результат**: `JobRunner.submit`, `resume`, `cancel`; семафор
    `MAX_CONCURRENT_ANALYSES`; при старте `running` переводится в `interrupted`; ошибка этапа →
    `failed` с кодом; прогресс этапа.
  - **Готово, когда**: на фейковых этапах — «крах» после этапа 2 и `resume` выполняет только
    этапы 3+; отмена работает; лимит параллелизма соблюдается.
  - **Тесты**: `test_runner.py`.

- [X] T076 Пул процессов для CPU-работы — `src/gr/pipeline/workers.py`
  - **Цель**: NFR-014: разбор, рендер страниц и проверка цитат не блокируют веб-сервер, а сбой
    на файле не роняет его.
  - **Зависит**: T003
  - **Файлы**: `pipeline/workers.py`, `settings.py` (`PARSE_WORKERS`, `PARSE_TIMEOUT_S`),
    `tests/integration/test_workers.py`
  - **Результат**: `CpuWorkers.run(fn, *args, timeout_s)`: отдельный spawn-процесс на задачу,
    не больше `PARSE_WORKERS` одновременно. Не `ProcessPoolExecutor`: там смерть одного воркера
    ломает весь пул и роняет параллельные задачи. Падение процесса → `WorkerCrashedError`
    только у этой задачи; таймаут → kill только своего процесса. PDFium используется только
    внутри воркеров.
  - **Готово, когда**: функция с `os._exit(1)` и бесконечный цикл дают ошибку задачи, а сервер и
    другие задачи продолжают работать; `/healthz` отвечает во время тяжёлой задачи.
  - **Тесты**: `test_workers.py`.

**Checkpoint**: каркас готов, user stories можно начинать.

---

## Phase 3: User Story 1 — Загрузка НПА и структура (P1) 🎯 MVP

**Goal**: загрузка PDF с текстовым слоем, DOCX и TXT, диагностика, дерево фрагментов с
метками, смещениями и страницами, реквизиты. Сканы, HTML, `.doc`/`.rtf`, очистка справочных
систем и законы — в Phase 7a, после сквозного сценария. Все адаптеры отдают единое
промежуточное представление (IR), структура выбирается соревнованием стратегий, инварианты
разбора проверяются на любом входе.

**Independent Test**: quickstart Q1–Q3.

- [X] T077 [US1] Промежуточное представление документа (IR) — `src/gr/ingestion/ir.py`
  - **Цель**: FR-017 (R-22): все форматы отдают одинаковые блоки с признаками оформления, а
    структура строится по IR.
  - **Зависит**: T007
  - **Файлы**: `ingestion/ir.py`, `ingestion/base.py`, `tests/unit/ingestion/test_ir.py`
  - **Результат**: модели `Block` и `RawDocument` (data-model §2), протокол
    `Extractor.extract(data) -> RawDocument`, функция `full_text(blocks)` с картой смещений
    блоков.
  - **Готово, когда**: модели валидируются (страница ≥ 1, корректный bbox); `full_text`
    детерминирован, по любому смещению находится блок.
  - **Тесты**: `test_ir.py`.

- [X] T015 [P] [US1] Определение формата и TXT — `src/gr/ingestion/detect.py`
  - **Цель**: FR-001, FR-002, FR-004 для TXT; протокол `Extractor` (FR-007).
  - **Зависит**: T007, T077
  - **Файлы**: `ingestion/base.py`, `detect.py`, `registry.py`, `txt.py`,
    `tests/unit/ingestion/test_detect_txt.py`
  - **Результат**: сигнатурное определение формата; `RawDocument`; TXT с charset-normalizer
    (UTF-8 и cp1251, низкая уверенность → `encoding_undetected`).
    Адаптер отдаёт блоки IR (T077): абзац TXT = блок.
  - **Готово, когда**: `.xlsx`, переименованный в `.txt`, → `unsupported_format`; cp1251
    читается; пустой файл → `empty_document`.
  - **Тесты**: `test_detect_txt.py`.

- [X] T016 [P] [US1] PDF — `src/gr/ingestion/pdf.py`
  - **Цель**: FR-001, FR-003, FR-004 для PDF (R-04).
  - **Зависит**: T015, T072, T076, T077
  - **Файлы**: `ingestion/pdf.py`, `tests/unit/ingestion/test_pdf.py`
    и `ingestion/pdf_layout.py`
  - **Результат**: постраничный текст через pypdfium2; `empty_pages`; статусы `ok`, `partial`,
    `needs_ocr`; `encrypted_file`, `corrupted_file`; лимит страниц.
    Строки собираются в абзацы (`pdf_layout.py`): перенос внутри абзаца склеивается, дефис на конце строки обрабатывается, новый абзац — по отступу, пустой строке или маркеру нумерации после завершённого предложения. PDFium вызывается только в пуле процессов.
    По итогам T072 ч. 3: строки берутся из `get_text_range` (PDFium восстанавливает строки и
    пробелы; сборка из фрагментов `get_rect` ломается на PDF, порезанных посреди слов), геометрия —
    из `get_charbox` символов строки. Начало абзаца — по отступу первой строки относительно поля;
    «слово-» + «продолжение» склеиваются без пробела с дефисом; `U+FFFE` внутри строки
    удаляется; «строка» правее конца предыдущей на той же высоте (после надстрочной сноски)
    приклеивается к ней.
    По итогам T072 ч. 2: знак переноса `U+FFFE` / `\x02` / `U+00AD` в конце строки удаляется со
    склейкой слова; фрагменты строки объединяются по базовой линии с допуском ~½ кегля;
    колонтитулы — повторяемость маскированного текста на ≥ 50 % страниц в верхних или нижних
    15 % страницы, отдельно для чётных и нечётных страниц; метаданные PDF не используются.
    Выход — блоки IR: bbox, отступ, кегль и жирность из текстовых объектов PDF.
  - **Готово, когда**: на синтетике — текстовый PDF `ok`, скан `needs_ocr`, битый
    `corrupted_file`; на `reg261.pdf` — `ok`, шапка КонсультантПлюс и «Страница N из M» не попали
    в блоки.
    Строка, начинающаяся с «10 дней» или «2020 г.», не становится отдельным абзацем.
  - **Тесты**: `test_pdf.py`.

- [X] T017 [P] [US1] DOCX с автонумерацией — `src/gr/ingestion/docx.py`
  - **Цель**: FR-001, ASU-8, R-05.
  - **Зависит**: T015, T072, T077
  - **Файлы**: `ingestion/docx.py`, `docx_numbering.py`, `tests/unit/ingestion/test_docx.py`
  - **Результат**: абзацы и таблицы по порядку; восстановление номеров из `numbering.xml`
    (`decimal`, `lowerLetter`, `russianLower`, `upperRoman`, `bullet`); флаг
    `numbering_restored`. Лимиты ZIP проверяются до разбора; OLE-контейнер → `encrypted_file`
    (R-04).
    Текст собирается обходом XML вместо `paragraph.text`: `w:t` с учётом `w:ins`, без `w:del`, включая гиперссылки и поля; надписи и сноски добавляются в конец с пометкой.
    Колонтитулы DOCX не читаются: в выгрузках справочных систем там баннер и номера
    страниц (T072).
    Выход — блоки IR: стиль абзаца, жирность, выравнивание, уровень списка, ячейки таблиц.
  - **Готово, когда**: текст DOCX с автонумерацией совпадает с текстом DOCX с ручной нумерацией
    той же синтетики; `reg261.docx` читается; синтетическая zip-бомба отклоняется с
    `corrupted_file` (`zip_limits`) без полной распаковки.
    В DOCX с режимом рецензирования вставленный текст есть, а удалённого нет.
  - **Тесты**: `test_docx.py`.

- [X] T019 [US1] Нормализация текста — `src/gr/grounding/normalize.py`
  - **Цель**: единая нормализация для смещений структуры и проверки цитат (R-11).
  - **Зависит**: T002
  - **Файлы**: `grounding/normalize.py`, `tests/unit/grounding/test_normalize.py`
  - **Результат**: `normalize_document_text` (сохраняет переносы абзацев, карта страниц) и
    `normalize_for_match` (регистр, ё/е, кавычки, тире, мягкие переносы, переносы слов).
  - **Готово, когда**: таблица кейсов проходит; функции идемпотентны.
  - **Тесты**: `test_normalize.py`.

- [X] T020 [US1] Построение структуры — `src/gr/structure/builder.py`
  - **Цель**: FR-010…FR-012 (R-06).
    Трассировка: FR-013.
  - **Зависит**: T019, T015, T072, T077
  - **Файлы**: `structure/patterns.py`, `builder.py`, `config/structure_patterns.toml`,
    `tests/unit/structure/test_builder.py`, `tests/unit/structure/test_reg261_structure.py`
  - **Результат**: дерево `Fragment` (преамбула/приказ, разделы, главы, подразделы, пункты
    `N.N.N`, подпункты `N)`, `а)`, перечни, абзацы, приложения); стабильные id и метки;
    смещения и страницы; режим `paragraphs`.
    Номер пункта принимается, только если правдоподобно продолжает нумерацию (за 3.5 — 3.5.1, 3.6 или 4.1, но не 10 и не 2020); иначе строка — продолжение текста. Повтор номера → id с суффиксом `~2` и предупреждение в диагностике.
    По итогам T072: продолжения заголовка раздела (абзацы со строчной буквы) склеиваются;
    ненумерованные подзаголовки — узлы `subsection`; дробные подпункты `3.1)`; блок
    «Утвержден … от … N …» — граница утверждаемого документа; контекст нумерации
    сбрасывается на разделе, приложении и утверждаемом документе; «Приложение:» без номера —
    не начало приложения.
    Работает по IR (T077) и задаёт каркас стратегий: `regulation` и `paragraphs`; выбор по оценке — T078.
  - **Готово, когда**:
    - инварианты вложенности соблюдены на всех фикстурах;
    - на синтетике точное дерево совпадает с golden;
    - на reg261 (если есть): разделы I–V, пункты 2.6, 2.16 (26 подпунктов), 3.2…3.8.3,
      приказ отделён от регламента;
    - `no_numbering.txt` → `paragraphs` с предупреждением.
    - дерево `reg261.pdf` совпадает с деревом `reg261.docx` (метки и порядок пунктов);
    - документ с повторяющейся нумерацией загружается без ошибки, все id уникальны.
  - **Тесты**: `test_builder.py`, `test_reg261_structure.py` (skip без фикстуры).

- [X] T078 [US1] Стратегии структуры и выбор по оценке — `src/gr/structure/strategies.py`
  - **Цель**: FR-018 (R-22): несколько стратегий соревнуются, побеждает лучшая; уверенность — в
    диагностике.
  - **Зависит**: T020
  - **Файлы**: `structure/strategies.py`, `structure/scoring.py`, `tests/fixtures/make_fixtures.py`
    (`styled_headings.docx`: заголовки стилем и жирностью без номеров),
    `tests/unit/structure/test_strategies.py`
  - **Результат**: стратегии `regulation`, `layout`, `paragraphs` (`law` добавляется в T068);
    оценка и выбор; `diagnostics.structure`; исключение внутри стратегии выводит её из
    соревнования с записью причины.
  - **Готово, когда**:
    - синтетический регламент → `regulation` с уверенностью ≥ 0,8;
    - `styled_headings.docx` → `layout`;
    - `no_numbering.txt` → `paragraphs` с низкой уверенностью и предупреждением;
    - стратегия, бросающая исключение, не ломает разбор.
  - **Тесты**: `test_strategies.py`.

- [X] T021 [P] [US1] Реквизиты документа — `src/gr/structure/metadata.py`
  - **Цель**: FR-006 (R-07).
  - **Зависит**: T020
  - **Файлы**: `structure/metadata.py`, `tests/unit/structure/test_metadata.py`
  - **Результат**: вид акта и орган, дата, номер, редакция, регистрация в Минюсте — каждый с
    цитатой и смещением.
  - **Готово, когда**: на reg261 — 11.09.2020, № 261, ред. 30.05.2023, Минюст 14.09.2020
    № 59820; на синтетике без реквизитов — `None` без ошибок.
  - **Тесты**: `test_metadata.py`.

- [X] T022 [US1] Сервис и API документов — `src/gr/services/documents.py`, `src/gr/api/documents.py`
  - **Цель**: эндпоинты api.md § Документы.
    Трассировка: FR-005, NFR-008.
  - **Зависит**: T016, T017, T020, T021, T011, T076
  - **Файлы**: `services/documents.py`, `api/documents.py`,
    `tests/integration/test_documents_api.py`
  - **Результат**: загрузка (лимиты, формат, извлечение, структура, реквизиты, дедупликация по
    sha256, сохранение); список, карточка, PATCH реквизитов, структура, фрагмент с контекстом.
    Размер проверяется по `Content-Length` и при потоковом приёме: обрыв при превышении, файл целиком в память не читается. Разбор идёт в пуле процессов.
  - **Готово, когда**: Q1–Q3 автоматизированы. Коды `413`, `415`, `422` и `X-Duplicate-Of` по
    контракту. Загрузка 100-страничного PDF ≤ 30 с (SC-001, тест с маркером `slow`).
    Файл 25 МБ отклоняется до полного приёма; во время разбора 100-страничного PDF `/healthz` отвечает ≤ 200 мс.
  - **Тесты**: `test_documents_api.py`.

- [X] T023 [US1] Экраны документов — `src/gr/web/templates/documents.html`, `document.html`
  - **Цель**: экраны 1, 2 из ui.md.
    Трассировка: FR-013.
  - **Зависит**: T022
  - **Файлы**: `web/routes.py`, `templates/documents.html`, `document.html`,
    `partials/fragment.html`, `partials/tree.html`, `tests/integration/test_web_documents.py`
  - **Результат**: форма загрузки с лимитами; список; реквизиты с цитатами и редактированием;
    диагностика; сворачиваемое дерево; просмотр фрагмента (HTMX).
  - **Готово, когда**: ошибки загрузки отображаются русским текстом из api.md; в HTML нет
    неэкранированного текста документа (тест с `<script>` в фикстуре).
  - **Тесты**: `test_web_documents.py`.

- [X] T081 [US1] Инварианты разбора на любом входе и fuzz — `tests/property/`
  - **Цель**: SC-014: на любом файле сервис не падает, текст не теряется, позиции и id
    валидны.
  - **Зависит**: T022, T078
  - **Файлы**: `tests/property/test_ingestion_invariants.py`, `tests/property/mutations.py`,
    `tests/property/test_upload_anything.py`
  - **Результат**: инварианты R-22 на синтетике, dev-корпусе (если есть) и мутациях с
    фиксированным seed; тест «загрузи что угодно» — zip, xlsx, pptx, изображение, случайные байты, пустой файл,
    текст в неизвестной кодировке → понятный код ошибки.
  - **Готово, когда**: 300 мутаций проходят все инварианты; ни одного ответа 500.
  - **Тесты**: сами файлы задачи.

**Checkpoint US1**: Q1–Q3 проходят (PDF, DOCX, TXT).

---

## Phase 4: User Story 2 — Карта процессов (P1) 🎯 MVP

**Goal**: S3–S5. Процессы и операции с проверенными цитатами, «Не подтверждено», покрытие,
UI карты процессов.

**Independent Test**: quickstart Q4, Q11, Q12, Q14; на FakeLLM — автоматически.

- [X] T024 [P] [US2] Проверка цитат — `src/gr/grounding/quotes.py`
  - **Цель**: FR-025, R-11 (статусы `verified`, `relocated`, `partial`, `unverified`,
    `invalid_quote`).
  - **Зависит**: T019, T020
  - **Файлы**: `grounding/quotes.py`, `config/grounding.toml`, `tests/unit/grounding/test_quotes.py`
  - **Результат**: `verify_quote(quote, fragment_id, fragments) -> GroundingResult`, поиск по
    всему документу для `relocated`.
    Цитата с «…» делится на части, каждая ищется по порядку в поддереве фрагмента. Перенесённая цитата ищется сначала точно по всему документу, затем нечётко — только среди кандидатов из n-граммного индекса.
  - **Готово, когда**: кейсы покрыты — точное совпадение, разница в «ё», кавычках и переносе,
    перефраз → `partial` или `unverified`, цитата из соседнего пункта → `relocated`, короткая
    или длинная → `invalid_quote`.
    Проверка 500 цитат на документе в 130 тыс. знаков занимает ≤ 5 с.
  - **Тесты**: `test_quotes.py`.

- [X] T025 [P] [US2] Русские числительные и сроки — `src/gr/grounding/deadlines.py`
  - **Цель**: проверка сроков (FR-025, FR-029).
  - **Зависит**: T019
  - **Файлы**: `grounding/numerals_ru.py`, `deadlines.py`, `tests/unit/grounding/test_deadlines.py`
  - **Результат**: `parse_deadline(quote) -> list[(value, unit)]`, `check_deadline(...)`.
  - **Готово, когда**: таблица проходит — «двадцати дней», «2 рабочих дня», «трёх рабочих дней»,
    «не позднее следующего рабочего дня», «15 минут», «10 календарных дней», «в течение
    одного месяца»; рассогласование значения → `deadline_not_in_quote`.
  - **Тесты**: `test_deadlines.py`.

- [X] T026 [P] [US2] Проверка исполнителя — `src/gr/grounding/roles.py`
  - **Цель**: FR-025 для ролей.
  - **Зависит**: T019
  - **Файлы**: `grounding/roles.py`, `tests/unit/grounding/test_roles.py`
  - **Результат**: `role_in_quote(role, quote) -> bool` (падежная эвристика R-11).
  - **Готово, когда**: «должностное лицо, ответственное за прием» находится в «должностным
    лицом, ответственным за прием»; «руководитель» не находится в цитате без него.
  - **Тесты**: `test_roles.py`.

- [X] T027 [P] [US2] LLM-схемы — `src/gr/extraction/schemas.py`
  - **Цель**: `DiscoveryOut`, `DetailOut`, `OpportunitiesOut` из llm.md.
    Трассировка: FR-046.
  - **Зависит**: T008
  - **Файлы**: `extraction/schemas.py`, `tests/unit/extraction/test_schemas_strict.py`
  - **Результат**: плоские схемы, совместимые со strict-режимом.
  - **Готово, когда**: тест генерирует JSON Schema и проверяет `additionalProperties: false`,
    все поля в `required`, отсутствие `oneOf`, `anyOf` (кроме `null`), `minLength`, `pattern`.
  - **Тесты**: `test_schemas_strict.py`.

- [X] T028 [P] [US2] Рендер документа для LLM и оглавление — `src/gr/extraction/render.py`
  - **Цель**: блок `<document>`, экранирование (R-12), оглавление и разбиение (R-10).
    Трассировка: NFR-004.
  - **Зависит**: T020
  - **Файлы**: `extraction/render.py`, `outline.py`, `config/chunking.toml`,
    `tests/unit/extraction/test_render.py`
  - **Результат**: `render_fragments(...)`, `build_outline(...) -> list[OutlineChunk]` с
    бюджетом символов и разбиением по разделам верхнего уровня.
    В промпте id фрагментов заменяются короткими алиасами `F<n>` с обратным отображением; неизвестный алиас в ответе даёт issue, а не исключение.
  - **Готово, когда**: строки `</document>` и `[fragment` из текста экранируются; оглавление
    больше бюджета режется без потери фрагментов; результат детерминирован.
  - **Тесты**: `test_render.py`.

- [X] T029 [US2] S3 Discover — `src/gr/extraction/discovery.py`
  - **Цель**: [llm.md § S3](./contracts/llm.md#s3--discover--карта-процессов).
  - **Зависит**: T024, T027, T028, T012
  - **Файлы**: `extraction/discovery.py`, `extraction/prompts/discover.md`,
    `tests/integration/extraction/test_discovery.py`, `tests/fixtures/llm/discover_*.json`
  - **Результат**: вызовы по чанкам оглавления, фильтрация несуществующих id, проверка
    evidence, слияние кандидатов по Жаккару, «процессы не найдены». Примеры в промпте — только из
    синтетики (R-21).
  - **Готово, когда**: на FakeLLM проверены сценарии «корректный», «несуществующие id»,
    «дубли из двух чанков», «пусто».
  - **Тесты**: `test_discovery.py`.

- [X] T030 [US2] S4 Detail — `src/gr/extraction/detailing.py`
  - **Цель**: [llm.md § S4](./contracts/llm.md#s4--detail--декомпозиция-процесса-до-операций).
    Трассировка: FR-021, FR-023, NFR-004.
  - **Зависит**: T029
  - **Файлы**: `extraction/detailing.py`, `extraction/prompts/detail.md`,
    `tests/integration/extraction/test_detailing.py`, `tests/fixtures/llm/detail_*.json`
  - **Результат**: вызов на процесс с `LLM_CONCURRENCY`; окна при превышении
    `DETAIL_MAX_CHARS`; сохранение по процессам (для возобновления); порог частичных ошибок 30 %.
    При `finish_reason=length` окно делится пополам и обрабатывается заново (глубина до 3). Цитаты в промпте ограничены 250 символами.
  - **Готово, когда**: на FakeLLM проверены сценарии «два окна склеены по порядку», «ошибка
    одного процесса из пяти → `completeness.complete=false` со списком необработанных», «три из пяти → `failed`».
    Сценарий FakeLLM «обрезанный ответ» → деление окна → успех.
  - **Тесты**: `test_detailing.py`.

- [X] T031 [US2] S5 Validate — `src/gr/validation/`
  - **Цель**: FR-024…FR-028 (llm.md § S4 «Валидация»).
    Трассировка: FR-023, FR-026.
  - **Зависит**: T024, T025, T026, T030
  - **Файлы**: `validation/process_validator.py`, `graph.py`, `dedupe.py`, `confidence.py`,
    `coverage.py`, `extraction/mapping.py`, `tests/unit/validation/test_*.py`
  - **Результат**: `ProcessModel{processes, unverified, coverage}`, стабильные id,
    `ValidationReport`.
  - **Готово, когда**:
    - «галлюцинация» (цитаты нет в тексте) → элемент в `unverified`, не в фактах;
    - срок «30 дней» при цитате «20 дней» → `deadline_not_in_quote`;
    - цикл без `is_loop_back` → issue;
    - дубли слиты;
    - покрытие считает непривязанные фрагменты с действиями;
    - близкое совпадение (`partial`) основной цитаты → элемент в `unverified`, не в фактах;
    - операция типа `decision`, для которой модель вернула `legally_significant=false`,
      получает `true`;
    - цитата из фрагмента с `text_source=ocr` → уверенность не выше `medium`;
    - фрагменты `repealed` не попадают в оглавление и в процессы.
  - **Тесты**: `test_process_validator.py`, `test_graph.py`, `test_confidence.py`,
    `test_coverage.py`.

- [X] T032 [US2] Запуск анализа и API процессов — `src/gr/services/analyses.py`, `src/gr/api/analyses.py`
  - **Цель**: api.md § Анализ (кроме перегенерации); подключение S3–S5 к runner.
  - **Зависит**: T014, T031, T022, T013, T076
  - **Файлы**: `pipeline/stages.py`, `services/analyses.py`, `api/analyses.py`,
    `tests/integration/test_analysis_e2e.py`
  - **Результат**: POST запуска (проверки: анализируемость, политика, `run_in_progress`),
    статус, resume, cancel, список версий, `processes`, `processes/{id}` (с `mermaid` из T033).
    `RunConfig` и учёт токенов.
  - **Готово, когда**: E2E на синтетике с FakeLLM — загрузка → анализ → процессы с
    проверенными цитатами. Q11 (`disabled` → 503) и Q12 (`local_only` + внешний хост → 503 без
    вызова) проходят. Число вызовов FakeLLM ожидаемое.
  - **Тесты**: `test_analysis_e2e.py`.

- [ ] T033 [P] [US2] Генерация Mermaid — `src/gr/analysis/mermaid.py`
  - **Цель**: детерминированная схема процесса (api.md § Mermaid).
  - **Зависит**: T008
  - **Файлы**: `analysis/mermaid.py`, `tests/unit/analysis/test_mermaid.py`
  - **Результат**: `process_to_mermaid(process) -> str`.
  - **Готово, когда**: golden-тест совпадает; подписи с `"`, `]`, `<script>`, переносами строк
    экранируются; отклонённые операции не выводятся.
  - **Тесты**: `test_mermaid.py`.

- [ ] T034 [US2] Экраны статуса и карты процессов — `src/gr/web/templates/run.html`, `processes.html`
  - **Цель**: экраны 2a и 3 из ui.md.
    Трассировка: FR-071.
  - **Зависит**: T032, T033
  - **Файлы**: `web/routes.py`, `templates/run.html`, `processes.html`, `partials/*`,
    `tests/integration/test_web_processes.py`
  - **Результат**: опрос статуса через HTMX, ошибки этапов, токены; список процессов,
    Mermaid (`securityLevel: "strict"`), таблица операций с бейджами слоя и цитатами, точки
    решения, блок «Не подтверждено», покрытие, баннер «Анализ неполный» при
    `completeness.complete=false`.
  - **Готово, когда**: на результате FakeLLM страница содержит все разделы ui.md № 3, а
    элементы `unverified` не выводятся в таблице фактов.
  - **Тесты**: `test_web_processes.py`.

- [ ] T035 [US2] Тест устойчивости к инъекциям — `tests/integration/test_injection.py`
  - **Цель**: SC-011, NFR-005.
  - **Зависит**: T032
  - **Файлы**: `tests/integration/test_injection.py`, `tests/fixtures/llm/injection_obedient.json`
  - **Результат**: «послушная» FakeLLM возвращает выдуманный процесс и сроки из инструкции.
  - **Готово, когда**: ни один выдуманный элемент не попадает в факты; в промпт, отправляемый
    FakeLLM, текст документа попадает только внутри экранированного блока.
  - **Тесты**: `test_injection.py`.

- [ ] T036 [US2] [manual] Eval-харнесс и эталонная разметка — `evals/`
  - **Цель**: eval-гейт (принцип V, R-17). Используется skill `/eval`.
  - **Зависит**: T032, T006, T071, T074
  - **Файлы**: `evals/dataset.jsonl`, `target.py`, `config.toml`, `metrics.py`, `run.py`,
    `golden/*.json`, `markers.toml`, `tests/unit/evals/test_metrics.py`
  - **Результат**: метрики — полнота и точность процессов, полнота операций, доля `verified`,
    сроки не из текста, покрытие, токены и латентность; агрегаты — среднее и минимум по
    документам. Эталонная разметка трёх документов eval-набора (reg261 + 2 другого типа и
    уровня, R-21) сделана вручную по тексту (процессы и их фрагменты, ключевые операции, сроки).
    Проверяет человек.
  - **Готово, когда**: метрики покрыты unit-тестами на игрушечных данных; `uv run python -m
    evals.run` работает на живой LLM; golden проверен экспертом.
  - **Тесты**: `test_metrics.py`.

**Checkpoint MVP (US1+US2)**: Q1–Q4, Q11, Q12, Q14. Первый прогон eval записан в `baseline.json`.

---

## Phase 5: User Story 3 — Инициативы (P2)

**Goal**: S6–S7. Проблемы, инициативы, tech guard, ограничение автономности, статус
допустимости, связи, экраны 4–5 без экономики.

**Independent Test**: quickstart Q5 на FakeLLM и на живой LLM.

- [ ] T037 [P] [US3] Каталог проблем и tech guard — `src/gr/analysis/tech_guard.py`
  - **Цель**: FR-043.
  - **Зависит**: T008
  - **Файлы**: `config/problem_catalog.toml`, `analysis/tech_guard.py`,
    `tests/unit/analysis/test_tech_guard.py`
  - **Результат**: паттерн → минимальная категория; `evaluate(initiative, findings) ->
    TechGuard`.
  - **Готово, когда**: LLM-инициатива для `completeness_check` или `duplicate_checks` →
    `requires_justification`; `classic_automation` → нет.
  - **Тесты**: `test_tech_guard.py`.

- [ ] T038 [P] [US3] Статус допустимости и ограничение автономности — `src/gr/analysis/gating.py`
  - **Цель**: FR-045, FR-063 (data-model §6).
  - **Зависит**: T008
  - **Файлы**: `analysis/gating.py`, `tests/unit/analysis/test_gating.py`
  - **Результат**: `cap_autonomy(...)`, `compute_gate(...)` с накоплением причин.
  - **Готово, когда**: каждое из 5 правил и их порядок покрыты тестами; `autonomous` на
    `decision` → `human_approval` + `autonomy_capped`.
  - **Тесты**: `test_gating.py`.

- [ ] T039 [P] [US3] Связи инициатив — `src/gr/analysis/relations.py`
  - **Цель**: FR-048.
  - **Зависит**: T008
  - **Файлы**: `analysis/relations.py`, `tests/unit/analysis/test_relations.py`
  - **Результат**: `alternative` или `complementary` по общим finding; компоненты альтернатив;
    переопределение экспертом.
  - **Готово, когда**: кейсы покрыты — общий finding, разные finding, цепочка из трёх
    альтернатив, переопределение.
  - **Тесты**: `test_relations.py`.

- [ ] T040 [US3] S6 Opportunities — `src/gr/extraction/opportunities.py`
  - **Цель**: [llm.md § S6](./contracts/llm.md#s6--opportunities--проблемы-и-инициативы).
    Трассировка: FR-042, FR-046.
  - **Зависит**: T027, T031, T037
  - **Файлы**: `extraction/opportunities.py`, `extraction/prompts/opportunities.md`,
    `tests/integration/extraction/test_opportunities.py`, `tests/fixtures/llm/opp_*.json`
  - **Результат**: вызов на процесс; на вход только эффективная модель (без полного текста) и
    каталог.
  - **Готово, когда**: на FakeLLM проверены «корректный», `no_change`, «ссылка на
    несуществующую операцию».
  - **Тесты**: `test_opportunities.py`.

- [ ] T041 [US3] S7 Postprocess и подключение к runner — `src/gr/analysis/postprocess.py`
  - **Цель**: llm.md § S6 «Валидация» (п. 1–6).
    Трассировка: FR-042, FR-044.
  - **Зависит**: T038, T039, T040
  - **Файлы**: `analysis/postprocess.py`, `pipeline/stages.py`,
    `tests/unit/analysis/test_postprocess.py`, обновление `test_analysis_e2e.py`
  - **Результат**: `InitiativeSet` в `stage_outputs`.
  - **Готово, когда**: инварианты data-model §4 выполняются на всех сценариях FakeLLM; E2E
    доходит до `postprocess=succeeded`.
  - **Тесты**: `test_postprocess.py`, `test_analysis_e2e.py`.

- [ ] T066 [US3] [manual] Eval этапа S6 — `evals/metrics_opportunities.py`
  - **Цель**: критерии качества из [llm.md § S6](./contracts/llm.md#s6--opportunities--проблемы-и-инициативы);
    eval-гейт для промпта S6 (принцип V).
  - **Зависит**: T036, T041
  - **Файлы**: `evals/metrics_opportunities.py`, `evals/judge.py`,
    `evals/prompts/judge_initiative.md`, `evals/config.toml`,
    `tests/unit/evals/test_metrics_opportunities.py`
  - **Результат**: детерминированные метрики (ссылочная целостность, доля LLM/agent-решений для
    паттернов с минимальной категорией `classic_automation`, наличие более простой
    альтернативы, доля `no_change`) и LLM-as-judge (обоснованность выбора технологии,
    конкретность) с кэшем вердиктов. Метрики S6 входят в `baseline.json`.
  - **Готово, когда**: метрики покрыты unit-тестами на игрушечных данных; регрессия S6
    относительно baseline даёт exit 1.
  - **Тесты**: `test_metrics_opportunities.py`.

- [ ] T042 [US3] API и экраны инициатив (без экономики) — `src/gr/api/results.py`, `initiatives.html`
  - **Цель**: api.md § Инициативы (без economics и priority), экраны 4 и 5 из ui.md.
    Трассировка: FR-071.
  - **Зависит**: T041, T034
  - **Файлы**: `services/results.py`, `api/results.py`, `templates/initiatives.html`,
    `initiative.html`, `tests/integration/test_initiatives_api.py`
  - **Результат**: таблица и карточка: процесс, проблема, решение, архитектура, альтернативы,
    регулирование, положения к пересмотру, статус допустимости, цитаты операций. Фильтры.
    Баннер «Анализ неполный» при неполном результате.
  - **Готово, когда**: Q5 на FakeLLM. Карточка содержит все поля FR-070, кроме экономики и
    приоритета (они помечены «будет рассчитано»).
  - **Тесты**: `test_initiatives_api.py`, `test_web_initiatives.py`.

**Checkpoint US3**: инициативы видны и объяснимы.

---

## Phase 6: User Story 4 — Экономика (P2)

**Goal**: допущения по умолчанию, формулы, сценарии, комбинирование, атрибуция, редактирование
с мгновенным пересчётом.

**Independent Test**: quickstart Q6, SC-005, SC-006.

- [ ] T043 [P] [US4] Профиль и генерация допущений — `src/gr/analysis/defaults.py`
  - **Цель**: FR-050…FR-052 (data-model §5).
  - **Зависит**: T008
  - **Файлы**: `config/assumption_profile.toml`, `analysis/defaults.py`,
    `tests/unit/analysis/test_defaults.py`
  - **Результат**: `build_assumptions(model, initiatives, profile, existing?) -> AssumptionSet`;
    тройки по коэффициентам разброса; `requires_input` для объёмов; значения пользователя
    не перезаписываются.
    Доля случаев по умолчанию — 1,0; для операций после точек решения — `requires_input=true`.
  - **Готово, когда**: для каждой операции, процесса и инициативы есть все ключи из
    data-model; источник `default_profile`; повторная генерация сохраняет `user`-значения.
  - **Тесты**: `test_defaults.py`.

- [ ] T044 [US4] Экономика: автономный эффект — `src/gr/analysis/economics.py`
  - **Цель**: FR-053, формулы spec.md для автономного (solo) эффекта; шаг 1 порядка вычислений
    (data-model §7).
  - **Зависит**: T043
  - **Файлы**: `analysis/economics.py`, `tests/unit/analysis/test_economics.py`,
    `tests/unit/analysis/test_economics_properties.py`
  - **Результат**: `compute_solo(model, initiatives, assumptions, scenario) -> EconomicsResult`
    на `Decimal` (`layer=assumption`, версия допущений): часы, FTE, деньги, opex, net,
    payback по месяцам, ROI, предупреждения. Портфель — в T065.
  - **Готово, когда**:
    - 3 вручную посчитанных примера совпадают до копейки;
    - отрицательный эффект считается честно;
    - property-тесты на 200 случайных наборах (seed фиксирован): независимость от порядка,
      идентичность повторов.
  - **Тесты**: `test_economics.py`, `test_economics_properties.py`.

- [ ] T045 [US4] API допущений и экономики — `src/gr/services/results.py`
  - **Цель**: api.md: `GET`/`PATCH /assumptions`, `GET /economics`, solo-экономика в
    `/initiatives`; FR-056, FR-057. Портфель добавляется в T065 и T049.
  - **Зависит**: T044, T042
  - **Файлы**: `services/results.py`, `api/results.py`, `pipeline/stages.py` (создание
    `AssumptionSet v1` в S7), `tests/integration/test_assumptions_api.py`
  - **Результат**: версии, `ETag`/`If-Match` → 409, валидация, предупреждения, `ReviewEvent`
    на изменение.
  - **Готово, когда**: PATCH возвращает пересчитанную таблицу; счётчик FakeLLM не изменился;
    время ответа ≤ 2 с на 100 инициатив (маркер `slow`); конфликт версий → 409.
  - **Тесты**: `test_assumptions_api.py`.

- [ ] T046 [US4] Экран настроек модели и экономика в таблице — `src/gr/web/templates/settings.html`
  - **Цель**: экран 6 из ui.md (часть с допущениями); экономика на экранах 4–5; баннеры
    FR-058.
  - **Зависит**: T045
  - **Файлы**: `templates/settings.html`, `initiatives.html`, `initiative.html`,
    `tests/integration/test_web_settings.py`
  - **Результат**: редактирование троек с источником и комментарием; пересчёт через HTMX;
    переключатель сценария; баннеры (итог портфеля — в T049).
  - **Готово, когда**: Q6 проходит вручную; формулировки денежных показателей соответствуют
    FR-058 (проверка тестом на наличие текста баннера).
  - **Тесты**: `test_web_settings.py`.

**Checkpoint US4**: AC-05, AC-06 (автономный эффект). Сценарии 3–4 US4 закрываются в T065.

---

## Phase 7: User Story 5 — Приоритизация (P2)

**Goal**: Priority Score, разбивка, группы, ранжирование, настраиваемые веса.

**Independent Test**: quickstart Q7.

- [ ] T047 [P] [US5] Скоринг — `src/gr/analysis/scoring.py`
  - **Цель**: FR-060…FR-062, FR-065 (data-model §6).
  - **Зависит**: T044
  - **Файлы**: `config/priority_default.toml`, `analysis/scoring.py`,
    `tests/unit/analysis/test_scoring.py`
  - **Результат**: нормирование критериев, `score`, `ease`, вклады, ранжирование с
    фиксированным tie-break; `blocked` без ранга.
  - **Готово, когда**: пересчёт по разбивке совпадает с `score`; все веса 0, кроме одного →
    балл = 100 × этот критерий; tie-break детерминирован; добавление инициативы не меняет балл
    других (FR-061).
  - **Тесты**: `test_scoring.py`.

- [ ] T048 [P] [US5] Группировка — `src/gr/analysis/grouping.py`
  - **Цель**: FR-064, правила spec.md.
  - **Зависит**: T047
  - **Файлы**: `analysis/grouping.py`, `tests/unit/analysis/test_grouping.py`
  - **Результат**: `assign_group(...) -> (group, rule_no, reason)`.
  - **Готово, когда**: таблица тестов срабатывает на каждом из 6 правил и на их порядке;
    каждая инициатива получает ровно одну группу.
  - **Тесты**: `test_grouping.py`.

- [ ] T065 [US5] Портфель и атрибуция — `src/gr/analysis/portfolio.py`
  - **Цель**: FR-054, FR-055, определение портфеля; шаг 3 порядка вычислений (data-model §7).
  - **Зависит**: T047, T048, T039
  - **Файлы**: `analysis/portfolio.py`, `tests/unit/analysis/test_portfolio.py`,
    `tests/unit/analysis/test_portfolio_properties.py`
  - **Результат**: `build_portfolio(solo, priorities, initiatives) -> PortfolioEconomics`: состав
    (без отклонённых, `blocked`, альтернатив с меньшим баллом и Not Recommended),
    мультипликативное комбинирование, атрибуция, итоги, `overlaps`.
  - **Готово, когда**:
    - 50 % + 40 % → 70 % (US4 сц. 3);
    - из группы альтернатив в портфеле одна — с лучшим баллом (US4 сц. 4);
    - инициатива из Not Recommended в портфель не входит;
    - property-тесты на 200 случайных портфелях (seed фиксирован): `Σ attributed = portfolio`,
      `combined ≤ 1`, независимость от порядка, идентичность повторов.
  - **Тесты**: `test_portfolio.py`, `test_portfolio_properties.py`.

- [ ] T049 [US5] API конфигурации приоритета и интеграция — `src/gr/api/results.py`
  - **Цель**: `GET`/`PUT /priority-config`; приоритет в `/initiatives` и карточке; экран 6
    (веса).
  - **Зависит**: T047, T048, T046, T065
  - **Файлы**: `services/results.py`, `api/results.py`, `templates/settings.html`,
    `initiatives.html`, `initiative.html`, `tests/integration/test_priority_api.py`
  - **Результат**: версии конфигурации, пересчёт, разбивка балла и правило группы в карточке.
    Итог портфеля и атрибутированный эффект — в таблице и карточке.
  - **Готово, когда**: Q7 автоматизирован: два одинаковых GET побайтно равны; смена весов и
    возврат восстанавливает порядок; `blocked` в конце с причиной.
  - **Тесты**: `test_priority_api.py`.

**Checkpoint US5**: AC-07. Сквозной сценарий без экспертной проверки готов.

---

## Phase 7a: Покрытие документов (US1, расширение)

**Goal**: документы, которые принесут тестировщики: выгрузки справочных систем, законы и
составные приказы, HTML, сканы, многоколоночные PDF, `.doc`/`.rtf`/`.odt`. Делается после того, как сквозной сценарий
работает на PDF, DOCX и TXT.

**Independent Test**: quickstart Q15–Q17; регрессия dev-корпуса (SC-012); `.doc`, `.rtf`, `.odt`.

- [ ] T018 [P] [US1] HTML (SHOULD) — `src/gr/ingestion/html.py`
  - **Цель**: BR-06, первый шаг расширения форматов.
  - **Зависит**: T015
  - **Файлы**: `ingestion/html.py`, `tests/unit/ingestion/test_html.py`
  - **Результат**: удаление `script`, `style`, `nav`, `header`, `footer`; блочные элементы → абзацы.
    Кодировка берётся из `<meta charset>` / `http-equiv`, затем из charset-normalizer.
    Выход — блоки IR с тегом (`h1`–`h6`, `li`, `p`, `td`).
  - **Готово, когда**: HTML-синтетика даёт тот же нормализованный текст, что и TXT-синтетика.
  - **Тесты**: `test_html.py`.

- [ ] T067 [US1] Очистка текста: справочные системы, пометки, колонтитулы — `src/gr/structure/cleanup.py`
  - **Цель**: FR-014, FR-015 (R-20).
  - **Зависит**: T020, T022
  - **Файлы**: `structure/cleanup.py`, `config/noise_patterns.toml`,
    `tests/unit/structure/test_cleanup.py`
  - **Результат**: удаление служебных строк справочных систем; «Список изменяющих документов» →
    метаданные редакции; пометки «(в ред. …)», «(введён …)» → `notes`; признак
    `repealed`; удаление колонтитулов и номеров страниц PDF, склейка пунктов через границу
    страницы; счётчики в `diagnostics.removed_noise`.
    По итогам T072: в DOCX служебные блоки КонсультантПлюс — таблицы 1×N (баннер, «Список
    изменяющих документов», «КонсультантПлюс: примечание.»). Такие таблицы распознаются по
    тексту и уходят в `notes` и метаданные редакции, а не в текст пунктов. Колонтитулы PDF
    удаляются по полосам сверху и снизу страницы плюс повторяемости.
  - **Готово, когда**: на `spps_export.docx` в тексте пунктов нет служебных строк и пометок, а
    пометки есть в `notes`; утративший силу пункт помечен `repealed`; на `headers.pdf` колонтитулов
    нет, разорванный пункт целый; синтетика без шума не меняется.
  - **Тесты**: `test_cleanup.py`.

- [ ] T068 [US1] Профили нумерации: законы, составные приказы, таблицы — `src/gr/structure/profiles.py`
  - **Цель**: FR-010, FR-011, FR-016 для актов, отличных от регламентов (R-06).
  - **Зависит**: T020, T067, T071, T078
  - **Файлы**: `structure/profiles.py`, `structure/builder.py`, `config/structure_patterns.toml`,
    `tests/unit/structure/test_profiles.py`, `tests/corpus/test_structure_regression.py`
  - **Результат**: автоматический выбор профиля `regulation` / `law` / `generic`; «§»; снимки
    структуры dev-корпуса. Уже сделано в T020: статьи и части («ч. 2 ст. 5», «п. 1 ч. 2 ст. 5»,
    «СТАТЬЯ 3_1»), «Раздел 1», несколько утверждаемых документов (в т. ч. «Приложение N к
    приказу») как отдельные корни с названием в метке, строки таблиц как абзацы с « | ».
    Оформляется как стратегия `law` и расширение `regulation` для составных приказов (T078).
  - **Готово, когда**:
    - на `law_like.txt`, `multi_approved.docx`, `table.docx` дерево совпадает с golden;
    - параметризованная регрессия по dev-корпусу: разбор без ошибок, снимки `*.structure.json`
      совпадают, снимки проверены человеком (SC-012).
  - **Тесты**: `test_profiles.py`, `test_structure_regression.py`.

- [ ] T069 [P] [US1] OCR-движок — `src/gr/ingestion/ocr.py`
  - **Цель**: FR-008 (R-04): распознавание страниц без текстового слоя.
  - **Зависит**: T016
  - **Файлы**: `ingestion/ocr.py`, `settings.py` (`OCR_*`), `tests/unit/ingestion/test_ocr.py`
  - **Результат**: протокол `OcrEngine`; реализация на tesseract — рендер страницы pypdfium2 в
    `OCR_DPI` в оттенках серого и передача в tesseract как PGM (без Pillow, R-22), вызов через `create_subprocess_exec` (без shell) с `OCR_LANG`, разбор TSV,
    уверенность страницы, таймаут на страницу; `ocr_available()` проверяет движок и язык `rus`.
  - **Готово, когда**:
    - на синтетическом `scan.pdf` CER ≤ 3 % (SC-013);
    - зашумлённая страница даёт низкую уверенность;
    - таймаут → ошибка страницы, а не зависание;
    - без `tesseract` в PATH → `unavailable`, без исключения.

    Тесты с реальным движком помечены маркером `ocr` и пропускаются, если движка нет; в
    Docker-образе и CI они выполняются.
  - **Тесты**: `test_ocr.py`.

- [ ] T070 [US1] OCR в потоке загрузки: фон, статусы, UI — `src/gr/services/documents.py`
  - **Цель**: FR-003, FR-008; api.md (`202`, `POST /documents/{id}/ocr`, `409 ocr_in_progress`,
    `readyz.ocr`); ui.md, экран 2.
  - **Зависит**: T069, T014, T022, T023, T067, T076
  - **Файлы**: `services/documents.py`, `api/documents.py`, `pipeline/runner.py` (вид задачи
    `ocr`, `OCR_CONCURRENCY`), `api/health.py`, `templates/document.html`,
    `tests/integration/test_ocr_flow.py`
  - **Результат**: скан → `202` и фоновая задача; в смешанном PDF OCR только для пустых страниц;
    после OCR — очистка и структура, фрагменты `text_source=ocr`; повтор OCR; прогресс и качество
    по страницам в UI; бейдж «OCR».
  - **Готово, когда**: с фейковым `OcrEngine` проверены сценарии:
    - скан → `done` → документ анализируем;
    - ошибка → `failed` → повтор;
    - движок недоступен → `needs_ocr`;
    - запуск анализа во время OCR → `409`.
  - **Тесты**: `test_ocr_flow.py`.

- [ ] T075 [US1] Конвертация `.doc`, `.rtf`, `.odt` — `src/gr/ingestion/convert.py`
  - **Цель**: FR-001: старые и смежные форматы, которые часто публикуют госсайты (R-04).
  - **Зависит**: T017, T073, T076
  - **Файлы**: `ingestion/convert.py`, `ingestion/detect.py`,
    `tests/fixtures/synthetic/sample.{doc,rtf,odt}`, `tests/unit/ingestion/test_convert.py`
  - **Результат**: определение формата по сигнатуре — OLE CFB с потоком `WordDocument` → `.doc`,
    с `EncryptionInfo` → `encrypted_file`; `{\rtf` → `.rtf`; ZIP с `content.xml` → `.odt`.
    Конвертация `soffice --headless --convert-to docx` в отдельном процессе с таймаутом и
    временным профилем, дальше — обычный путь DOCX. Без LibreOffice → понятная ошибка
    «сохраните документ как DOCX».
  - **Готово, когда**: синтетические `.doc`, `.rtf`, `.odt` дают тот же текст, что и исходный
    DOCX; зависшая конвертация завершается по таймауту и не вешает сервис. Тесты с LibreOffice
    помечены маркером `soffice` и пропускаются, если его нет; в Docker-образе и CI выполняются.
  - **Тесты**: `test_convert.py`.

- [ ] T079 [US1] Многоколоночная вёрстка PDF — `src/gr/ingestion/pdf_layout.py`
  - **Цель**: FR-019 (R-22).
  - **Зависит**: T016, T077
  - **Файлы**: `ingestion/pdf_layout.py`, `tests/fixtures/make_fixtures.py` (`two_columns.pdf`),
    `tests/unit/ingestion/test_pdf_columns.py`
  - **Результат**: колонки определяются по распределению левых координат фрагментов; чтение —
    колонка за колонкой; страницы с колонками — в `diagnostics.columns_pages`.
  - **Готово, когда**: текст `two_columns.pdf` читается в исходном порядке; одноколоночные
    страницы не меняются.
  - **Тесты**: `test_pdf_columns.py`.

**Checkpoint 7a**: Q15–Q17 проходят, регрессия dev-корпуса зелёная, старые форматы
конвертируются.

---

## Phase 8: User Story 6 — Экспертная проверка (P3)

**Goal**: подтверждение, правка, отклонение, история, `stale`, перегенерация.

**Independent Test**: quickstart Q8.

- [ ] T050 [US6] Наложение решений — `src/gr/analysis/overlay.py`
  - **Цель**: FR-080…FR-083 (data-model §8).
    Трассировка: FR-081.
  - **Зависит**: T008, T041
  - **Файлы**: `analysis/overlay.py`, `tests/unit/analysis/test_overlay.py`
  - **Результат**: `apply_reviews(outputs, events) -> EffectiveState` (модель, инициативы,
    статусы, `stale`); белые списки полей.
  - **Готово, когда**: функция идемпотентна; порядок по `created_at`; `reject` исключает из
    расчётов; правка операции даёт `stale` у зависимых инициатив; патч вне белого списка →
    ошибка; переходы статусов по диаграмме.
  - **Тесты**: `test_overlay.py`.

- [ ] T051 [US6] API ревью и наложение во всех чтениях — `src/gr/api/reviews.py`
  - **Цель**: api.md § Экспертная проверка; все read-пути используют эффективное состояние.
    Трассировка: SC-010.
  - **Зависит**: T050, T049
  - **Файлы**: `services/reviews.py`, `api/reviews.py`, `services/results.py`,
    `tests/integration/test_reviews_api.py`
  - **Результат**: POST и GET ревью; процессы, инициативы и экономика с учётом решений;
    ответ со `stale_initiative_ids`.
  - **Готово, когда**: Q8 автоматизирован; решения переживают перезапуск приложения (новый
    `create_app` на той же БД).
  - **Тесты**: `test_reviews_api.py`.

- [ ] T052 [US6] Перегенерация инициатив процесса — `src/gr/services/analyses.py`
  - **Цель**: FR-083, api.md `regenerate-initiatives`.
  - **Зависит**: T051
  - **Файлы**: `services/analyses.py`, `api/analyses.py`, `pipeline/stages.py`,
    `tests/integration/test_regenerate.py`
  - **Результат**: S6 и S7 для одного процесса, версия `opportunities@N`, старая сохранена;
    `stale` снят.
  - **Готово, когда**: на FakeLLM новая версия эффективна, старая доступна, остальные процессы
    не тронуты; решения по старым инициативам видны как «по предыдущей версии»;
    пользовательские допущения перенесены на инициативу с теми же операциями и категорией.
  - **Тесты**: `test_regenerate.py`.

- [ ] T053 [US6] UI проверки и история — `src/gr/web/templates/history.html`
  - **Цель**: кнопки проверки на экранах 3 и 5; экран 7.
  - **Зависит**: T051
  - **Файлы**: `templates/processes.html`, `initiative.html`, `history.html`,
    `partials/review_form.html`, `tests/integration/test_web_reviews.py`
  - **Результат**: подтвердить, исправить, отклонить (с обязательным комментарием); бейджи
    статусов; лента истории «было → стало».
  - **Готово, когда**: отклонение без комментария не отправляется (сервер возвращает 400 и
    сообщение).
  - **Тесты**: `test_web_reviews.py`.

**Checkpoint US6**: BR-05.

---

## Phase 9: User Story 7 — Экспорт (P3)

**Goal**: JSON по схеме, CSV инициатив и операций.

**Independent Test**: quickstart Q9.

- [ ] T054 [P] [US7] Экспорт JSON и CSV — `src/gr/export/`
  - **Цель**: FR-092, FR-093, api.md § Экспорт.
  - **Зависит**: T051
  - **Файлы**: `export/json_export.py`, `csv_export.py`, `tests/unit/export/test_export.py`
  - **Результат**: `AnalysisExport` (`schema_version 1.0`) с тремя сценариями экономики; CSV
    UTF-8 с BOM и `;`.
  - **Готово, когда**: JSON валиден по схеме из Pydantic; у каждого элемента есть `layer` и
    `review.status`; числа CSV равны таблице `/initiatives` для того же сценария.
  - **Тесты**: `test_export.py`.

- [ ] T055 [US7] API и кнопки экспорта — `src/gr/api/exports.py`
  - **Цель**: эндпоинты экспорта, схема, кнопки на экране 4.
  - **Зависит**: T054
  - **Файлы**: `api/exports.py`, `templates/initiatives.html`, `tests/integration/test_exports_api.py`,
    `tests/contract/test_openapi_contract.py`
  - **Результат**: `Content-Disposition`; `/api/v1/schema/analysis-export.json`. Contract-тест:
    маршруты и коды OpenAPI соответствуют contracts/api.md.
  - **Готово, когда**: Q9 автоматизирован; contract-тест зелёный.
  - **Тесты**: `test_exports_api.py`, `test_openapi_contract.py`.

---

## Phase 10: User Story 8 — Развёртывание (P3)

**Goal**: AC-10, NFR-007, NFR-013 на сервере.

**Independent Test**: quickstart «Запуск (сервер)», Q13.

- [ ] T056 [US8] Docker и доставка через `/deploy-kit` — `Dockerfile`, `docker-compose.yml`
  - **Цель**: R-16. Изменение CI и деплоя — согласовать до выполнения.
  - **Зависит**: T032, T073
  - **Файлы**: `Dockerfile`, `compose.prod.yml`, `.github/workflows/*`, runbook (генерирует
    deploy-kit); `docker-compose.yml` (AC-10, профиль `local-llm`); `.dockerignore`
  - **Результат**: образ на uv, non-root, `HEALTHCHECK /healthz`, том `/data`, один воркер;
    в образе `tesseract-ocr` и `tesseract-ocr-rus`.
    Права на том `/data` выставляются для пользователя приложения; `HEALTHCHECK` через `python -c` (без curl); `OMP_THREAD_LIMIT=1`; `libreoffice-writer-nogui` для конвертации; размер образа и время OCR — в runbook.
  - **Готово, когда**: локально `docker compose up -d --build` → `/healthz` 200; при политике
    `disabled` загрузка работает, а анализ даёт `llm_not_configured`; с реальным endpoint анализ
    проходит.
  - **Тесты**: `tests/integration/test_container_smoke.sh` (ручной запуск).

- [ ] T057 [US8] Устойчивость к перезапуску — `tests/integration/test_restart_resume.py`
  - **Цель**: NFR-013, Q13.
  - **Зависит**: T032, T014
  - **Файлы**: `tests/integration/test_restart_resume.py`
  - **Результат**: E2E «крах» посреди S4, новый `create_app` на той же БД → `interrupted` →
    resume. Готовые процессы S4 не перезапрашиваются (счётчик FakeLLM).
  - **Готово, когда**: тест зелёный; ручная проверка `docker compose restart app` описана в
    runbook.
  - **Тесты**: `test_restart_resume.py`.

- [ ] T058 [P] [US8] README и инструкция развёртывания — `README.md`, `docs/deploy.md`
  - **Цель**: Phase 6 постановки.
  - **Зависит**: T056
  - **Файлы**: `README.md`, `docs/deploy.md`
  - **Результат**: назначение, архитектура (ссылки на specs), локальный запуск (включая
    `brew install tesseract tesseract-lang`), настройка
    LLM API (политики, примеры OpenRouter, Ollama, vLLM), пользователи
    (`gr hash-password`), требования к серверу, TLS через реверс-прокси, обновление, бэкап тома,
    healthcheck, ограничения MVP.
    Пример конфигурации реверс-прокси: `client_max_body_size 25m`, `proxy_read_timeout 120s`, заголовки `X-Forwarded-*`, `APP_PUBLIC_URL`.
  - **Готово, когда**: человек, не участвовавший в разработке, разворачивает сервис по
    инструкции на чистой VM (SC-009, [manual]).
  - **Тесты**: —

---

## Phase 11: Validation & Polish

- [ ] T059 [manual] Eval и настройка промптов на eval-наборе — `evals/baseline.json`
  - **Цель**: SC-002…SC-004 на трёх документах eval-набора.
  - **Зависит**: T036, T041, T066
  - **Файлы**: `extraction/prompts/*.md` (только общие правки, без упоминаний конкретного
    документа), `evals/baseline.json`, `README.md` (токены и стоимость)
  - **Результат**: метрики ≥ порогов SC-003 по среднему и по минимуму по документам
    eval-набора; доля `verified` ≥ 95 %; сроки не из текста = 0;
    метрики S6 (T066) не хуже baseline.
    Выбрана самая дешёвая модель, прошедшая eval.
  - **Готово, когда**: `baseline.json` закоммичен; T060 зелёный с маркерами всех документов
    eval-набора и отложенной выборки (`evals/markers.toml`).
  - **Тесты**: eval.

- [ ] T061 [manual] Валидация на отложенной выборке — `docs/validation-report.md`
  - **Цель**: AC-08, SC-004, SC-007.
  - **Зависит**: T059, T060, T006
  - **Файлы**: `docs/validation-report.md`, `evals/golden/holdout.json` (разметка после прогона,
    только для отчёта)
  - **Результат**: полный цикл без изменений кода на каждом документе отложенной выборки
    (≥ 3, R-21); метрики; ручной аудит фактов; качество
    рекомендаций; корректность приоритизации; найденные ошибки и ограничения; предложения
    по развитию.
  - **Готово, когда**: отчёт содержит метрики обоих документов и список ограничений MVP.
  - **Тесты**: —

- [ ] T062 [P] [manual] Пример результата по reg261 — `docs/examples/reg261/`
  - **Цель**: артефакт 9 постановки.
  - **Зависит**: T059
  - **Файлы**: `docs/examples/reg261/{export.json, initiatives.csv, operations.csv, screens/*.png}`
  - **Результат**: экспорт и скриншоты экранов 2–6.
  - **Готово, когда**: файлы на месте, JSON валиден по схеме.
  - **Тесты**: —

- [ ] T063 Прогон quickstart и сценариев отказа — `specs/001-npa-ai-analyzer/quickstart.md`
  - **Цель**: SC-001, SC-005, SC-008.
  - **Зависит**: T061
  - **Файлы**: отметки в `quickstart.md` (дата и результат по каждому Q)
  - **Результат**: Q1–Q19 проходят; замеры времени записаны.
  - **Готово, когда**: все Q отмечены как пройденные или имеют задокументированное ограничение.
  - **Тесты**: —

- [ ] T064 Финальная проверка согласованности и PR — `/speckit-analyze`, `/pr-ready`
  - **Цель**: соответствие артефактов (spec ↔ plan ↔ tasks ↔ код) и конституции.
  - **Зависит**: T063
  - **Файлы**: —
  - **Результат**: отчёт `/speckit-analyze` без критичных находок; `/pr-ready` зелёный; PR по
    согласованию.
  - **Готово, когда**: `check.sh` зелёный, mypy strict зелёный, PR готов к созданию.
  - **Тесты**: полный прогон.

---

## Dependencies & Execution Order

```text
Setup (T001–T006, T060, T071)
   └─▶ Спайки (T072, T073; T074 — после выбора провайдера)
          └─▶ Foundational (T007–T014, T076)
                 └─▶ US1 ядро (T015–T017, T019–T023, T077, T078, T081) ─▶ US2 (T024–T036)
                        └─▶ US3 (T037–T042, T066) ─▶ US4 (T043–T046) ─▶ US5 (T047–T049, T065)
                               ═══ сквозной сценарий на PDF/DOCX/TXT ═══
                               ├─▶ Phase 7a: покрытие документов (T018, T067–T070, T075, T079)
                               ├─▶ US6 (T050–T053) ─▶ US7 (T054–T055)
                               └─▶ US8 (T056–T058; T056 можно начать после T032)
                                      └─▶ Validation (T059, T061–T064)
```

- US2 требует US1 (фрагменты). US3 — модель процессов US2. US4 — инициативы US3. US5 —
  экономику US4.
- Порядок экономики: T044 (solo) → T047, T048 (балл, группа) → T065 (портфель). Цикла нет: балл
  считается по solo-эффекту, а состав портфеля зависит от балла.
- Сквозной сценарий собирается до расширения форматов: Phase 7a идёт после US5, чтобы к этому
  моменту весь путь «загрузка → рейтинг» уже работал на PDF, DOCX и TXT.
- T067 (очистка) → T068 (законы, составные приказы). T071 (dev-корпус) нужен для регрессии в
  T068 и для eval-набора в T036.
- T076 (пул процессов) — до любого разбора на сервере: T022, T032, T070, T075.
- T077 (IR) — первая задача US1: все адаптеры отдают IR, очистка и структура работают по
  нему. T078 (стратегии) идёт после каркаса T020, T081 (инварианты и fuzz) — после API
  загрузки T022.
- T074 заблокирована выбором провайдера. До него вся разработка идёт на FakeLLM; T036 и T059
  требуют T074 и ключ.
- T060 и тест согласованности (`test_spec_consistency.py`) работают с Phase 1 и ловят код под
  конкретный документ, неизвестные зависимости, циклы и нетрассированные требования.
- T006 (отложенная выборка) — до T059: она фиксируется до настройки промптов.

### Parallel Opportunities

- **Setup**: T002–T006, T060, T071 параллельно после T001 (T060 — после T002).
- **Спайки**: T072 ∥ T073.
- **Foundational**: T008 ∥ T010 ∥ T012 ∥ T076 (после T007 и T003); T013 и T014 после T009.
- **US1**: T077 → T015, затем T016 ∥ T017; T019 параллельно; T078 после T020; T081 после T022.
- **US2**: T024 ∥ T025 ∥ T026 ∥ T027 ∥ T028 ∥ T033, затем T029 → T030 → T031 → T032.
- **US3**: T037 ∥ T038 ∥ T039, затем T040 → T041 → T042 ∥ T066.
- **US5**: T047 → T048 → T065 (T047, T048 параллельно с T046).
- **Phase 7a**: T018 ∥ T067 ∥ T069 ∥ T075 ∥ T079; затем T068, T070.
- **US7/US8**: T054 ∥ T057; T058 ∥ T062.

### Example: параллельный старт US2

```text
T024 grounding/quotes.py      T025 grounding/deadlines.py    T026 grounding/roles.py
T027 extraction/schemas.py    T028 extraction/render.py      T033 analysis/mermaid.py
```

## Implementation Strategy

0. **Спайки** (T072, T073 — около дня; T074 — после выбора провайдера): проверить на реальных
   данных разбор PDF и DOCX, образ с OCR и LibreOffice, структурированный вывод.
1. **Сквозной сценарий как можно раньше**: US1 ядро → US2 → US3 → US4 → US5 на PDF, DOCX и TXT с
   FakeLLM. После US5 работает весь путь «загрузка → карта процессов → инициативы → экономика →
   рейтинг», а главный риск R-01 (галлюцинации) проверен на проверке цитат.
2. **Покрытие документов** (Phase 7a): очистка справочных систем, законы и составные приказы,
   HTML, OCR, `.doc`/`.rtf`/`.odt`.
3. **Доверие и передача** (US6, US7): экспертная проверка и экспорт (BR-05, AC-09).
4. **Эксплуатация** (US8): Docker, README, деплой (AC-10). T056 можно начать сразу после T032.
5. **Валидация**: eval на трёх документах, отложенная выборка, отчёт, пример (AC-08).

Каждый checkpoint — точка демонстрации и коммита. `check.sh` включает тест согласованности
артефактов. Изменения промптов и моделей после первого baseline проходят только через eval.

## Summary

| Фаза | Задачи | Кол-во |
|---|---|---|
| Setup | T001–T006, T060, T071 | 8 |
| Спайки | T072–T074 | 3 |
| Foundational | T007–T014, T076 | 9 |
| US1 ядро (P1) | T015–T017, T019–T023, T077, T078, T081 | 11 |
| US2 (P1) | T024–T036 | 13 |
| US3 (P2) | T037–T042, T066 | 7 |
| US4 (P2) | T043–T046 | 4 |
| US5 (P2) | T047–T049, T065 | 4 |
| Покрытие документов | T018, T067–T070, T075, T079 | 7 |
| US6 (P3) | T050–T053 | 4 |
| US7 (P3) | T054–T055 | 2 |
| US8 (P3) | T056–T058 | 3 |
| Validation | T059, T061–T064 | 5 |
| **Итого** | | **80** (из них [manual]: T006, T036, T059, T061, T062, T066, T071, T072, T074, частично T058) |

T065 и T066 добавлены по итогам `/speckit-analyze`, T067–T071 — для тестирования на разнотипных
НПА и OCR, T072–T076 — по итогам pre-mortem, T077–T079 и T081 — универсальный разбор (IR, стратегии, колонки, инварианты; T080 «изображения»
исключена по решению заказчика). Номера существующих задач сохранены, поэтому ID идут
не по порядку фаз.
