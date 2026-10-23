# Data Model: AI-анализ НПА

**Feature**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md)

Доменные модели — Pydantic v2 (`src/gr/domain/`), `frozen=True`, `extra="forbid"`. Ниже указаны
поля, типы, правила валидации и связи. Схемы ответов LLM (`src/gr/extraction/schemas.py`) —
отдельный слой, они описаны в [contracts/llm.md](./contracts/llm.md). Перевод LLM-схем в
доменные модели делает детерминированный маппер.

Обозначения: `?` — опционально (`None`), `[]` — список, `→` — ссылка по id.

## 1. Перечисления (`domain/enums.py`)

| Enum | Значения | Назначение |
|---|---|---|
| `KnowledgeLayer` | `fact`, `interpretation`, `hypothesis`, `assumption` | Слой знаний (принцип III) |
| `ReviewStatus` | `pending`, `confirmed`, `edited`, `rejected` | Статус проверки (FR-080) |
| `GroundingStatus` | `verified`, `relocated`, `partial`, `unverified`, `invalid_quote` | Результат проверки цитаты (R-11). Проверку проходят только `verified` и `relocated` |
| `Confidence` | `high`, `medium`, `low` | Детерминированная уверенность (FR-027) |
| `DocFormat` | `pdf`, `docx`, `txt`, `html`, `doc`, `rtf`, `odt` | `doc`/`rtf`/`odt` конвертируются в DOCX |
| `TextLayerStatus` | `ok`, `partial`, `needs_ocr`, `empty` | Диагностика текстового слоя до OCR (FR-003) |
| `OcrStatus` | `not_needed`, `queued`, `running`, `done`, `failed`, `unavailable` | FR-008 |
| `TextSource` | `native`, `ocr` | Происхождение текста фрагмента |
| `NumberingProfile` | `regulation`, `law`, `generic` | Профиль нумерации (R-06) |
| `FragmentKind` | `root`, `preamble`, `approved_document`, `section`, `chapter`, `paragraph_section` (§), `subsection`, `article`, `part`, `point`, `subpoint`, `list_item`, `paragraph`, `appendix` | FR-010 |
| `StructureMode` | `numbered`, `paragraphs` | FR-012 |
| `OperationType` | `intake`, `registration`, `completeness_check`, `data_verification`, `interagency_request`, `document_preparation`, `approval`, `decision`, `signing`, `notification`, `issuance`, `record_keeping`, `control`, `other` | Справочник типов операций (FR-022) |
| `DeadlineUnit` | `minutes`, `hours`, `working_days`, `calendar_days`, `days`, `months`, `next_working_day`, `other` | `days` — если тип дней не указан |
| `ProblemPattern` | `manual_document_processing`, `duplicate_data_entry`, `lookup_and_reconciliation`, `request_classification`, `template_document_preparation`, `duplicate_checks`, `long_approvals`, `interdepartmental_handoff`, `large_text_analysis`, `other` | FR-040 |
| `SolutionCategory` | `classic_automation`, `ai_nlp`, `llm_rag`, `ai_agent`, `hybrid`, `no_change` | FR-041; `no_change` — «оптимизация не требуется» |
| `RegulatoryMode` | `within_current`, `requires_change` | BR-03 |
| `AutomationLevel` | `assistive`, `human_approval`, `autonomous` | FR-045 |
| `Level5` | `very_low`, `low`, `medium`, `high`, `very_high` | Качественные оценки |
| `DurationBucket` | `lt_3m`, `m3_6`, `m6_12`, `gt_12m` | Срок внедрения |
| `LegalGate` | `allowed`, `allowed_with_hitl`, `requires_regulatory_change`, `blocked` | FR-063 |
| `InitiativeRelation` | `alternative`, `complementary` | FR-048 |
| `PriorityGroup` | `quick_win`, `strategic`, `complex`, `not_recommended` | FR-064 |
| `Scenario` | `conservative`, `base`, `optimistic` | FR-051 |
| `AssumptionSource` | `default_profile`, `user`, `document` | FR-051 |
| `RunStatus` | `queued`, `running`, `succeeded`, `failed`, `interrupted`, `cancelled` | |
| `StageName` | `discover`, `detail`, `validate`, `opportunities`, `postprocess` | S3–S7 |
| `StageStatus` | `pending`, `running`, `succeeded`, `failed`, `skipped` | |

## 2. Документ и структура

### `Document`
| Поле | Тип | Правила |
|---|---|---|
| `id` | `str` (`doc_<ulid>`) | |
| `filename` | `str` | ≤ 255, без путей |
| `format` | `DocFormat` | по сигнатуре |
| `size_bytes` | `int` | ≤ `MAX_UPLOAD_BYTES` |
| `sha256` | `str` | уникален; повторная загрузка → существующий документ |
| `uploaded_at`, `uploaded_by` | `datetime`, `str` | |
| `page_count?` | `int` | для PDF |
| `char_count` | `int` | ≤ `MAX_CHARS` |
| `diagnostics` | `IngestionDiagnostics` | |
| `metadata` | `DocumentMetadata` | |
| `structure_mode` | `StructureMode` | |
| `user_source?`, `relevance_checked_at?`, `relevance_note?` | `str`, `date`, `str` | вводит пользователь (FR-006) |

### `Block` (промежуточное представление, R-22)
| Поле | Тип | Примечания |
|---|---|---|
| `text` | `str` | текст блока (абзац, строка таблицы, элемент списка) |
| `page?` | `int` | ≥ 1; для форматов без страниц — `None` |
| `bbox?` | `(x0, y0, x1, y1)` | PDF и OCR, в пунктах страницы |
| `indent?` | `float` | отступ первой строки относительно поля (PDF) |
| `font_size?`, `bold?` | `float`, `bool` | |
| `align?` | `left` / `center` / `right` / `justify` | |
| `style?` | `str` | имя стиля DOCX («Heading 1», «Заголовок 2») |
| `list_level?`, `list_marker?` | `int`, `str` | восстановленная нумерация (R-05) |
| `table?` | `{table: int, row: int, col: int}` | ячейка таблицы |
| `html_tag?` | `str` | `h1`…`h6`, `li`, `p`, `td` |
| `text_source` | `TextSource` | `native` / `ocr` |

`RawDocument = {format: DocFormat, blocks: list[Block], page_count?: int, diagnostics:
IngestionDiagnostics}`.

### `IngestionDiagnostics`
`text_layer: TextLayerStatus`, `empty_pages: list[int]`, `encoding?: str`, `warnings: list[str]`,
`numbering_restored: bool` (DOCX, R-05), `numbering_profile: NumberingProfile`,
`ocr: {status: OcrStatus, pages: list[int], mean_confidence?: float,
low_confidence_pages: list[int], engine_version?: str, error?: str}`,
`removed_noise: {spps_lines: int, header_footer_lines: int, editorial_notes: int}` — служебные
строки справочных систем и пометки (`structure/cleanup.py`, T067), колонтитулы PDF (адаптер),
`analyzable: bool` — ложно при `needs_ocr` или `empty` без успешного OCR и пока OCR не завершён.
`structure: {strategy: str, confidence: float, scores: dict[str, float], failed_strategies:
dict[str, str]}` (R-22), `columns_pages: list[int]` — страницы с многоколоночной вёрсткой.

### `DocumentMetadata`
Поля `act_type`, `issuer`, `date`, `number`, `title`, `edition_date`, `minjust_registration`.
Каждое имеет тип `MetaField = {value: str | None, citation?: Citation, source: "document" | "user"}`.

### `Fragment`
| Поле | Тип | Правила |
|---|---|---|
| `id` | `str` | путь, стабилен для одного текста: `body/III/3.5/3.5.1/sp2`; при повторе номера — суффикс `~2`; уникален в документе |
| `doc_id` | `str` → Document | |
| `kind` | `FragmentKind` | |
| `number?` | `str` | «3.5.1», «2)», «а)», «III» |
| `label` | `str` | «п. 3.5.1», «пп. 2 п. 2.8», «абз. 3 п. 3.5.1», «разд. III» |
| `heading?` | `str` | заголовок раздела или подраздела |
| `text` | `str` | собственный текст узла (без потомков) |
| `start`, `end` | `int` | смещения в полном нормализованном тексте, `start < end` |
| `pages` | `list[int]` | для PDF; пусто для прочих |
| `parent_id?` | `str` | |
| `order` | `int` | порядок в документе (обход в глубину) |
| `text_source` | `TextSource` | `ocr` — текст получен распознаванием (FR-008) |
| `repealed` | `bool` | «утратил силу» / «исключён»: в анализ не идёт (FR-014); по собственному тексту единицы после номера или по пометке «(Раздел/Статья/Пункт … утратил силу …)» после неё (T067) |
| `notes` | `list[str]` | редакционные пометки, вырезанные из текста (FR-014): у единицы, после которой пометка стоит, или у названной в ней («(п. 2.6 в ред. …)», «(Статья в редакции …)»); примечание системы — у следующей единицы; список изменяющих документов — у фрагмента, после которого стоит (акт, утверждаемый документ) |
| `approved_doc?` | `str` | короткое название утверждаемого документа, если их несколько |

Инвариант: интервалы потомков вложены в интервал родителя, `order` монотонен по `start`.

### `Citation`
`fragment_id: str`, `quote: str`, `grounding: GroundingResult`.

`GroundingResult = {status: GroundingStatus, score: float (0..100), matched_start?: int,
matched_end?: int, original_fragment_id?: str (если relocated), reason?: str}`.

## 3. Модель процессов (`domain/process.py`)

### `Grounded[T]` — значение с основанием
`value: T`, `citations: list[Citation]` (≥ 1 для слоя `fact`), `layer: KnowledgeLayer`,
`confidence: Confidence`. Если хотя бы одна цитата ссылается на фрагмент с
`text_source=ocr`, `confidence` не выше `medium` (FR-008).

### `Process`
| Поле | Тип | Слой |
|---|---|---|
| `id` | `str` (`proc_<n>` в рамках запуска) | |
| `name` | `Grounded[str]` | fact |
| `goal` | `Grounded[str]?` | fact или interpretation |
| `trigger` | `Grounded[str]?` | fact |
| `participants` | `list[Role]` | fact |
| `inputs` | `list[DocumentRef]` | fact |
| `operations` | `list[Operation]` | ≥ 1 |
| `decision_points` | `list[DecisionPoint]` | |
| `deadlines` | `list[Deadline]` | fact (уровень процесса, например общий срок услуги) |
| `information_systems` | `list[Grounded[str]]` | fact |
| `result` | `Grounded[str]?` | fact; отсутствие → issue `missing_result` |
| `related_process_ids` | `list[str]` | interpretation |
| `validation` | `ValidationReport` | |
| `confidence` | `Confidence` | вычисляется |
| `layer` | `interpretation` | сам процесс — факты акта, сведённые в модель (FR-093, T054); поля с основанием несут свой слой |
| `review` | `ReviewState` | |

`Role = {id, name: Grounded[str]}`. `DocumentRef = {name: Grounded[str], mandatory?: bool}`.

### `Operation`
| Поле | Тип | Примечания |
|---|---|---|
| `id` | `str` (`proc_3.op_5`) | |
| `seq` | `int` | порядок по основному пути |
| `name` | `str` | краткое название (interpretation) |
| `action` | `Grounded[str]` | fact: что делается |
| `object?` | `str` | над чем |
| `performer` | `Grounded[str]?` | fact; отсутствие → «не указано в НПА» |
| `inputs`, `outputs` | `list[Grounded[str]]` | fact |
| `depends_on` | `list[str]` → Operation | interpretation |
| `deadline?` | `Deadline` | fact |
| `conditions` | `list[Grounded[str]]` | fact |
| `information_systems` | `list[Grounded[str]]` | fact |
| `operation_type` | `OperationType` | interpretation |
| `legally_significant` | `bool` | interpretation; детерминированно `llm_flag OR operation_type ∈ {decision, signing}`; эксперт может снять признак только с комментарием |
| `handles_personal_data` | `bool` | interpretation |
| `is_loop_back` | `bool` | возврат на доработку (разрешённый цикл) |
| `confidence`, `review` | | |
| `layer` | `interpretation` | как у процесса |

### `Deadline`
`raw: Grounded[str]` (дословная формулировка), `value?: int`, `unit: DeadlineUnit`,
`anchor?: str` (от какого события), `parsed_from_quote: bool`. Правило R-11: при
`value != None` число и единица MUST разбираться из цитаты, иначе статус `unverified`.

### `DecisionPoint`
`id`, `after_operation_id`, `condition: Grounded[str]`,
`outcomes: list[{label: str, next_operation_id?: str, ends_process: bool}]` (≥ 2),
`criteria: list[Grounded[str]]` (например, основания отказа), `layer = interpretation`.

### `ValidationReport`
`issues: list[ValidationIssue]`, где `ValidationIssue = {code, severity: "error" | "warning",
element_id, message}`. Коды:
`missing_result`, `no_operations`, `dangling_dependency`, `cycle`, `unverified_quote`,
`deadline_not_in_quote`, `performer_not_in_quote`, `duplicate_merged`, `relocated_citation`,
`invalid_decision_point` (меньше двух исходов или нет операции, после которой принимается
решение).

### `ProcessModel` (результат S5 на запуск)
`processes: list[Process]`, `unverified: list[UnverifiedItem]`, `coverage: CoverageReport`.
`UnverifiedItem = {element_type, element_id, process_id, value, citation?, reason}` — утверждение
модели, не прошедшее проверку: показывается в блоке «Не подтверждено» и не используется как факт.

### `CoverageReport` (на запуск)
`fragments_total`, `fragments_used`, `uncovered_action_fragments: list[str]` — фрагменты, которые
содержат глаголы действия из словаря, но не вошли ни в один процесс. Плюс
`non_process_fragments` (отмечены discovery как не описывающие процессы).

## 4. Оптимизация (`domain/initiative.py`)

### `Finding` (проблемное место)
`id`, `process_id`, `operation_ids: list[str]` (≥ 1, существующие), `pattern: ProblemPattern`,
`description: str`, `layer = hypothesis`, `review`.

### `Initiative`
| Поле | Тип | Правила |
|---|---|---|
| `id` | `str` (`ini_<n>`) | |
| `process_id` | `str` | |
| `finding_ids` | `list[str]` | ≥ 1, кроме `no_change` |
| `operation_ids` | `list[str]` | ≥ 1, существующие, неотклонённые |
| `title`, `description` | `str` | |
| `solution_category` | `SolutionCategory` | |
| `architecture_idea` | `str` | компоненты, данные, точки контроля |
| `tech_rationale` | `str` | почему эта технология |
| `alternatives_considered` | `list[{category: SolutionCategory, why_not: str}]` | ≥ 1, кроме `no_change` |
| `regulatory_mode` | `RegulatoryMode` | |
| `provisions_to_revise` | `list[{fragment_id, change_nature: str}]` | ≥ 1, если `requires_change` |
| `requires_legal_review` | `bool` | `true`, если `requires_change` (инвариант) |
| `automation_level` | `AutomationLevel` | после ограничения (FR-045) |
| `automation_level_proposed` | `AutomationLevel` | исходное значение от LLM |
| `autonomy_capped` | `bool` | |
| `reduction_potential` | `list[{operation_id, level: Level5, justification}]` | по каждой операции из `operation_ids` |
| `ratings` | `Ratings` | |
| `risks`, `limitations` | `list[str]` | |
| `tech_guard` | `{min_category: SolutionCategory, requires_justification: bool}` | FR-043 |
| `gate` | `{status: LegalGate, reasons: list[str]}` | вычисляется |
| `relations` | `list[{initiative_id, relation: InitiativeRelation, source: "auto" | "expert"}]` | FR-048 |
| `layer` | `hypothesis` | |
| `stale` | `bool` | «требует пересмотра» (FR-083) |
| `review` | `ReviewState` | |

`Ratings`: поля `feasibility`, `data_availability`, `integration_complexity`, `legal_risk`,
`operational_risk` — каждое `{level: Level5, justification: str}`. Плюс
`duration: {bucket: DurationBucket, justification: str}`.

**Порядок категорий** (для tech guard): `no_change` < `classic_automation` < `ai_nlp` <
`hybrid` < `llm_rag` < `ai_agent`.

Каталог `config/problem_catalog.toml` задаёт минимальную категорию каждого `ProblemPattern`
и отдельные минимумы для типов операций (например, `completeness_check`). Для инициативы
берётся наибольший минимум среди связанных проблем и затронутых операций; остальные проблемы
и операции не влияют на оценку. `requires_justification` выставляется при превышении этого
минимума и сохраняется независимо от наличия текста обоснования. Числовой штраф применяется
в S9 по `PriorityConfig`, без изменения исходной качественной оценки LLM.

### `InitiativeVersion` (вывод S7 перегенерации, T052)
`process_id`, `version ≥ 2`, `based_on` (решения эксперта до этого момента — в её модели),
`result: InitiativeSet` (только этот процесс), `predecessors: dict[новая → заменённая]`,
`without_successor: list[str]`. Правила — §8.

## 5. Допущения (`domain/assumptions.py`)

### `Triple`
`conservative: Decimal`, `base: Decimal`, `optimistic: Decimal`, все ≥ 0. Для долей — ≤ 1.

### `Assumption`
`key: str` (например `op.proc_3.op_5.time_hours`), `scope: "global" | "process" | "operation" |
"initiative" | "initiative_operation"`, `target_id?: str`, `unit: str`, `values: Triple`,
`source: AssumptionSource`, `source_citation?: Citation`, `requires_input: bool`, `note?: str`,
`updated_by`, `updated_at`, `layer = assumption` (при любом источнике: параметр модели, FR-093).

### Ключи
| Scope | Ключ | Единица |
|---|---|---|
| global | `hourly_cost` | ₽/ч |
| global | `overhead_ratio` | доля |
| global | `fte_hours_per_year` | ч |
| global | `horizon_years` | 1–3 (целое, одно значение во всех сценариях) |
| process | `volume_per_year` | случаев/год |
| process | `hourly_cost_override?` | ₽/ч |
| operation | `time_hours` | ч на выполнение |
| operation | `case_share` | доля |
| initiative_operation | `reduction_share` | доля |
| initiative_operation | `review_hours` | ч на выполнение |
| initiative_operation | `variable_cost_per_exec` | ₽ |
| initiative | `capex` | ₽ |
| initiative | `fixed_opex_per_year` | ₽/год |
| initiative | `adoption_y1`, `adoption_y2`, `adoption_y3` | доля |

Формат ключа: `hourly_cost` (global), `proc.<process_id>.<параметр>`,
`op.<operation_id>.<параметр>`, `ini.<initiative_id>.<параметр>`,
`ini.<initiative_id>.<operation_id>.<параметр>`; `target_id` пары инициатива–операция —
`<initiative_id>/<operation_id>`. Параметр — последний сегмент ключа (в идентификаторе
операции есть точка). Конструкторы ключей — в `domain/assumptions.py`.

### `AssumptionSet`
`run_id`, `version: int` (монотонно), `items: list[Assumption]`, `created_by`, `created_at`.
Каждое сохранение пользователя создаёт новую версию. Текущая — максимальная.

### Профиль допущений по умолчанию

Файл `config/assumption_profile.toml`. **Все значения — `[Предположение]`**, плейсхолдеры для
замены данными организации, источник `default_profile`.

| Параметр | База | Консерв. / Оптим. |
|---|---|---|
| `hourly_cost` | 1 200 ₽/ч | ×1,0 / ×1,0 |
| `overhead_ratio` | 0,3 | = |
| `fte_hours_per_year` | 1 970 | = |
| `horizon_years` | 3 | = |
| `volume_per_year` | 1 000 (`requires_input=true`) | ×0,7 / ×1,3 |
| `case_share` | 1,0 для всех операций; для операций после точки решения — `requires_input=true` | = |
| `time_hours` по `OperationType` | intake 0,25; registration 0,1; completeness_check 0,5; data_verification 1,0; interagency_request 0,5; document_preparation 2,0; approval 0,5; decision 0,5; signing 0,1; notification 0,25; issuance 0,25; record_keeping 0,1; control 1,0; other 0,5 | ×0,8 / ×1,2 |
| `reduction_share` по `Level5` | very_low 0,05; low 0,15; medium 0,35; high 0,6; very_high 0,8 | ×0,6 / ×1,2 (≤ 1) |
| `review_hours` = доля × `time_hours` по категории | classic 0; ai_nlp 0,15; hybrid 0,2; llm_rag 0,25; ai_agent 0,3 | ×1,5 / ×0,7 |
| `capex` по категории × множитель сложности интеграции | classic 1,5 млн; ai_nlp 3 млн; hybrid 4 млн; llm_rag 4 млн; ai_agent 6 млн; множитель very_low 0,5; low 0,8; medium 1,0; high 1,5; very_high 2,2 | ×1,4 / ×0,8 |
| `fixed_opex_per_year` | 20 % от `capex` | ×1,3 / ×0,8 |
| `variable_cost_per_exec` по категории | classic 0; ai_nlp 0,5 ₽; hybrid 2 ₽; llm_rag 5 ₽; ai_agent 15 ₽ | ×1,5 / ×0,7 |
| `adoption_y1/y2/y3` | 0,5 / 0,9 / 1,0 | ×0,6 / ×1,2 (≤ 1) |

Генерация (`analysis/defaults.py`, T043) заполняет только отсутствующие ключи: значения
пользователя, из документа и ранее созданные по профилю не меняются. Производное значение
нового ключа (`review_hours` от `time_hours`, `fixed_opex_per_year` от `capex`) считается от
текущего значения родителя, в том числе введённого пользователем. Значения по профилю для
исчезнувших элементов отбрасываются, значения пользователя сохраняются. Если новых ключей
нет, версия не создаётся. Операция «после точки решения» — последователь операции, за
которой стоит точка, или цель её исхода (с последователями), позже точки в порядке
выполнения; зависимость от шага возврата (`is_loop_back`) ветку не продолжает. Инициатива
`no_change` не несёт затрат и сокращения (нули).

## 6. Приоритизация (`domain/priority.py`)

### `PriorityConfig` (версионируется так же, как допущения)
```text
weights: {effect: 30, feasibility: 20, data_availability: 15,
          integration: 10, risk: 15, duration: 10}         # ≥ 0, сумма > 0
level_map:    {very_low: 0, low: 0.25, medium: 0.5, high: 0.75, very_high: 1}
duration_map: {lt_3m: 1.0, m3_6: 0.7, m6_12: 0.4, gt_12m: 0.1}
effect_ref: 5_000_000            # ₽, накопленный базовый эффект за горизонт → s_effect = 1
tech_guard_penalty: 0.25         # вычитается из s_feasibility при requires_justification
cutoff_score: 35
ease_threshold: 0.6
effect_threshold: 0.4
```

### Нормирование критериев
- `s_effect = clamp(CumNet_base_solo / effect_ref, 0, 1)`
- `s_feasibility = max(0, level_map[feasibility] − penalty·[requires_justification])`
- `s_data = level_map[data_availability]`
- `s_integration = 1 − level_map[integration_complexity]`
- `s_risk = 1 − max(level_map[legal_risk], level_map[operational_risk])`
- `s_duration = duration_map[duration]`
- `score = round_half_even(100 × Σ w_k·s_k / Σ w_k, 1)`
- `ease = mean(s_feasibility, s_integration, s_duration)`

Реализация — `analysis/scoring.py` (T047), значения по умолчанию — `config/priority_default.toml`
(версия 1 конфигурации запуска). `raw` критерия: эффект — `CumNet_base_solo` (Decimal),
риск — худший из двух уровней, остальные — уровень или корзина срока из S6. Вклад критерия
`100·w·s/Σw` хранится с полной точностью: сумма вкладов — балл до округления. До группировки
результат — `ScoreCard` (критерии, балл, `ease`, эффект, статус допустимости); группу добавляет
`grouping.py` (T048). Ранг: округлённый балл по убыванию, затем эффект по убыванию, затем
`initiative_id` (FR-065); `blocked` без ранга и не занимает место. `grouping.build_priorities`
собирает `PriorityResult`: балл, ранг и группу с номером правила и причиной с числами, по
которым принято решение; порядок — по рангу, `blocked` в конце.

### `PriorityResult` (вычисляемый)
`initiative_id`, `criteria: dict[str, {raw, normalized, weight, contribution}]`, `score`,
`ease`, `group: PriorityGroup`, `group_rule: int`, `group_reason: str`, `gate: LegalGate`,
`rank?: int` (`None` для `blocked`), `layer = hypothesis` (производное от качественных
оценок, принцип III), `assumptions_version: int`, `priority_config_version: int`.

### Правила статуса допустимости (`analysis/gating.py`)
По порядку, причины накапливаются:
1. эксперт установил `blocked` → `blocked`;
2. `ratings.legal_risk = very_high` → `blocked`, причина: «оценка юридического риска —
   критическая; требуется юридическая экспертиза»;
3. `regulatory_mode = requires_change` → `requires_regulatory_change`;
4. затронута операция с `legally_significant` или `handles_personal_data`, либо
   `autonomy_capped` → `allowed_with_hitl`;
5. иначе → `allowed`.

Ограничение автономности: если `automation_level_proposed = autonomous` и среди затронутых
операций есть операция с `legally_significant`, то `automation_level = human_approval` и
`autonomy_capped = true`.

### Связи инициатив (FR-048, `analysis/relations.py`)
Для пары инициатив с общей операцией: общая `finding_id` → `alternative`, иначе →
`complementary`. Эксперт может переопределить связь. В портфель из компоненты альтернатив
(связность по `alternative`) входит инициатива с максимальным `score`, при равенстве — по
правилу FR-065. Остальные помечаются `excluded_as_alternative`.

## 7. Экономический результат (`analysis/economics.py`, вычисляемый)

```text
EconomicsResult
  scenario: Scenario
  layer: assumption            # производные от допущений (принцип III)
  assumptions_version: int
  per_initiative: list[InitiativeEconomics]
  portfolio: {initiative_ids, total: Effect | None}   # сумма атрибутированных; None — пуст
  overlaps: list[{operation_id, initiative_ids, combined_reduction[y]}]
  warnings: list[str]       # requires_input, сценарии не по порядку и т. п.

InitiativeEconomics
  initiative_id
  solo:       Effect
  assumption_keys: list[str]   # допущения, по которым посчитан эффект (FR-070)
  attributed: Effect | None    # None, если не в портфеле (alternative/blocked/rejected)
  in_portfolio: bool
  excluded_reason?: str
Effect = {hours[y], fte[y], money[y], opex[y], capex, net[y], cum_net, roi?, payback_months?}
```

Модели — `domain/economics.py`; T044 даёт `scenario`, `layer`, `assumptions_version`,
`per_initiative[].solo` и `warnings`, портфельные поля (`attributed`, `in_portfolio`,
`portfolio`, `overlaps`) приходят с `build_portfolio` (T065). Значения хранятся с полной
точностью (`Decimal`, 28 знаков), округление — только при выводе. Отсутствующий ключ или
нулевой `fte_hours_per_year` → `AssumptionValidationError` с перечнем ключей (400), ноль
не подставляется. Окупаемость считается в двенадцатых долях, без деления на 12, поэтому
сравнение с нулём точное. Предупреждения: неуказанные объёмы процессов, доли случаев после
точек решения (`requires_input`) и сценарии не по порядку — у параметров, увеличивающих
эффект (объём, доли, время, ставка, сокращение, внедрение), консервативное ≤ базовое ≤
оптимистичное; у затрат (capex, opex, переменные расходы, время проверки) — наоборот. В
расчёт и предупреждения идут только допущения, которые использованы.

**Порядок вычислений** (без циклов):
1. `compute_solo` (`analysis/economics.py`) — автономный эффект всех неотклонённых инициатив;
2. скоринг и группы по solo-эффекту (`scoring.py`, `grouping.py`);
3. `build_portfolio` (`analysis/portfolio.py`) — состав портфеля, комбинирование, атрибуция.

**Портфель** = не отклонены экспертом, `gate ≠ blocked`, не `excluded_as_alternative`,
группа ≠ `not_recommended`. Состав один во всех сценариях: он берётся из приоритетов
базового сценария. Из компоненты альтернатив входит лучшая по рангу из ещё допустимых
(недопустимая или Not Recommended альтернатива не оставляет компоненту пустой); остальные
получают `excluded_reason` «Альтернатива инициативе … с лучшим баллом». Реализация —
`analysis/portfolio.py`: `build_portfolio(initiatives, assumptions, solo, priorities) ->
EconomicsResult` дополняет автономный результат сценария составом, `attributed`,
итогом и `overlaps`; входы по операциям читаются тем же `economics.read_terms`.

Формулы — в [spec.md → Экономические формулы](./spec.md#экономические-формулы). Свойства,
которые проверяются тестами (property-based на случайных портфелях):
- `Σ attributed.hours = portfolio.hours`;
- `combined_reduction ≤ 1`;
- результат не зависит от порядка инициатив во входе;
- повторный вызов возвращает идентичный результат.

## 8. Экспертная проверка (`domain/review.py`)

### `ReviewEvent` (только на добавление)
`id`, `run_id`, `entity_type: "process" | "operation" | "decision_point" | "finding" |
"initiative" | "relation" | "assumption_set" | "priority_config"`, `entity_id`,
`action: "confirm" | "edit" | "reject" | "reopen" | "set_gate"`,
`patch?: dict[str, JSON]` (путь поля → новое значение), `previous?: dict[str, JSON]`,
`comment?: str` (обязателен для `reject`), `user`, `created_at`.

### `ReviewState` (на элементе, вычисляется наложением)
`status: ReviewStatus`, `last_event_id?`, `edited_fields: list[str]`.

### Наложение (`analysis/overlay.py`)
`effective_model = apply_reviews(stage_output, events_sorted_by_created_at)`. Это чистая функция.
- `edit` патчит только разрешённые поля (белый список на тип сущности). Отредактированные поля
  получают `layer = fact` с пометкой «исправлено экспертом», если эксперт приложил цитату, и
  `interpretation` в остальных случаях.
- `reject` исключает элемент из расчётов.
- Правка процесса или операции ставит `stale = true` у инициатив, которые на них ссылаются.
- Перегенерация (FR-083): события по инициативам предыдущей версии остаются в журнале и
  показываются блоком «Решения по предыдущей версии». Допущения с источником `user`
  переносятся на новую инициативу при совпадении множества `operation_ids` и
  `solution_category`. Непереносимые перечисляются в отчёте перегенерации.

Перегенерация (T052, `pipeline/outputs.py`, `analysis/versions.py`) — отдельный запуск
анализа (§9: `parent_run_id`, `process_id`): S3 и S4 пропущены; его S5 — процесс таким, каким
его оставил эксперт (наложение журнала, без отклонённого); S6 — только по этому процессу; S7 —
`InitiativeVersion` (§4). Версия 1 — сам анализ; следующая — `2, 3, …` по процессу, id новых
инициатив и проблем — `ini_<proc>_v<N>_<n>`, `fnd_<proc>_v<N>_<n>`. Действует последняя
версия каждого процесса; анализ читает свои S5 и S7 и S7 успешных перегенераций, прежние
версии читаются через `initiative-versions`. Предшественник новой инициативы — инициатива
действующей версии (с решениями эксперта: исправленная категория, операции без отклонённых)
с тем же множеством операций и той же категорией, один к одному в порядке id;
`without_successor` — прежние инициативы без пары. Значения пользователя переносятся при
чтении: недостающий ключ новой инициативы берёт значение `user` того же ключа
предшественника (по цепочке версий), иначе — профиль; сама перегенерация допущений не пишет.
«Требует пересмотра» у инициатив версии `N` ставят только решения, принятые после начала её
S5 (`based_on`; решения до этого уже в её модели), у версии 1 — любые. Решения по прежним
инициативам наложение пропускает; новые решения по ним — `404`. Ответ без допустимых
инициатив и без вывода «оптимизация не требуется» — ошибка перегенерации (`llm_invalid_output`),
действует прежняя версия. Отклонённая инициатива не помечается «требует пересмотра».

Реализация — `analysis/overlay.py` (T050): `apply_reviews(model, initiatives, events) ->
EffectiveState` (`model`, `findings`, `initiatives`, `excluded`, `stale_initiative_ids`;
`calculable()` и `calculation_model()` — без отклонённого). События — по `created_at`, затем
`id`; повтор `id` учитывается один раз; события по элементам, которых нет в текущих выходах
(прежняя версия инициатив), пропускаются. Белые списки `edit`:
- `process`: `name`, `goal`, `trigger`, `result`;
- `operation`: `name`, `action`, `performer`, `object`, `deadline`, `operation_type`,
  `legally_significant`, `handles_personal_data`, `is_loop_back`;
- `decision_point`: `condition`; `finding`: `pattern`, `description`;
- `initiative`: `title`, `description`, `architecture_idea`, `tech_rationale`,
  `solution_category`, `automation_level` (предложенный; ограничение применяется заново),
  `ratings` (частично: `{критерий: {level|bucket, justification}}`), `risks`, `limitations`;
- `relation` (`entity_id` = два id инициатив по алфавиту через «|»): только `edit` с
  `{relation: alternative|complementary}`.

Поле с основанием (`name`, `action`, `performer`, `condition`, …) принимает строку или
`{value, citation}`; цитата проверяется при записи решения (T051), без указания фрагмента — по
всему акту (T053). `deadline` —
`{value, unit, anchor?, raw?}` или `null`. `set_gate` — только у инициативы, `patch.status` —
`LegalGate`; переопределяет только `blocked`. Отклонённый процесс исключает свои операции,
точки решения и проблемы; инициатива теряет ссылки на отклонённые операции и проблемы, а без
операций (или без проблем, кроме `no_change`) исключается из расчётов с причиной. `stale` —
после `edit`, `reject`, `reopen` процесса, операции или проблемы, на которые опирается
инициатива. Пересчитываются ограничение автономности, tech guard, статус допустимости и связи.

Чтение (T051, `services/analyses.py` `outputs()`): каждое чтение карты процессов, инициатив,
экономики и настроек накладывает журнал на выходы S5 и S7. Экономика, приоритет и портфель
считаются по `calculation_model()` и `calculable()`; экран настроек показывает допущения того
же состава. Ключи допущений строятся по сохранённым выходам, а не по эффективному состоянию:
решение не добавляет и не удаляет ключей и не создаёт версии набора, а значения элемента,
возвращённого через `reopen`, сохраняются. Правка категории или оценок инициативы не
пересчитывает уже созданные значения профиля: их меняет пользователь на экране настроек или
перегенерация (T052). Запись решения (`services/reviews.py`): цитата проверяется по тексту
акта и хранится с результатом проверки (`relocated` — с исправленным фрагментом); решение
пишется, только если весь журнал с ним применяется без ошибки, и решения одного запуска
пишутся по одному (блокировка в процессе: приложение работает одним процессом). Иначе одно
неприменимое событие сломало бы каждое чтение запуска. Пара связи хранится по алфавиту; при
наложении пара в любом порядке — одна связь, действует последнее решение.

### Переходы статуса
```text
pending ──confirm──▶ confirmed ──edit──▶ edited
   │                    │                  │
   ├──edit──▶ edited ◀──┘                  │
   └──reject─▶ rejected ◀──reject──────────┘
rejected/confirmed/edited ──reopen──▶ pending
```

## 9. Запуск анализа (`domain/run.py`)

### `AnalysisRun`
`id` (`run_<ulid>`), `doc_id`, `parent_run_id?`, `process_id?` (оба — у перегенерации
инициатив процесса анализа `parent_run_id`, §8; в списке версий документа её нет),
`status: RunStatus`, `created_by`,
`created_at`, `stages: list[StageState]`, `config_snapshot: RunConfig`, `usage: UsageTotals`,
`error?: {code, message}`,
`completeness: {complete: bool, failed_items: list[{stage, process_key, error_code}]}`.
`complete=false`, если этап завершился с частичными ошибками (≤ 30 % процессов). UI показывает
баннер «Анализ неполный» на экранах результата (NFR-003).

`StageState = {name: StageName, status: StageStatus, started_at?, finished_at?, attempts,
error?, progress?: {done, total}}`.

`RunConfig`: модели по этапам, `prompt_versions: dict[str, str]`, `structured_mode`,
`temperature`, лимиты чанкинга, пороги проверки цитат, хэш профиля допущений.

### Переходы
```text
queued → running → succeeded
                 ↘ failed      (ошибка этапа; «Повторить» → running с первого незавершённого этапа)
                 ↘ interrupted (перезапуск сервиса; «Продолжить» → running)
queued|running → cancelled
```

## 10. Хранилище (`storage/db.py`, SQLAlchemy Core)

| Таблица | Ключевые колонки | Примечания |
|---|---|---|
| `meta` | `key`, `value` | версия схемы |
| `documents` | `id` PK, `sha256` UNIQUE, `payload` JSON | `Document` целиком |
| `fragments` | `doc_id`, `id`, `order`, `payload` JSON; PK(`doc_id`, `id`) | |
| `document_texts` | `doc_id` PK, `full_text`, `page_offsets` JSON | |
| `analysis_runs` | `id` PK, `doc_id`, `status`, `created_at`, `payload` JSON | |
| `stage_outputs` | `run_id`, `stage`, `payload` JSON, `created_at`; PK(`run_id`, `stage`) | неизменяемы после `succeeded` |
| `stage_items` | `run_id`, `stage`, `item_key`, `payload` JSON, `created_at`; PK(`run_id`, `stage`, `item_key`) | результат одного элемента этапа (процесса в S4) для возобновления; пишется один раз |
| `llm_calls` | `id`, `run_id`, `stage`, `model`, `attempt`, `status`, `in_tokens`, `out_tokens`, `latency_ms`, `request_hash`, `response` JSON (сырой ответ модели; хранится всегда, в логи не пишется), `created_at` | NFR-002/011, FR-030 |
| `llm_cache` | `request_hash` PK, `response` JSON, `model`, `created_at` | NFR-012; хэш = sha256(model, prompt_version, schema_hash, messages) |
| `review_events` | `id` PK, `run_id`, `entity_type`, `entity_id`, `created_at`, `payload` JSON | только INSERT |
| `assumption_sets` | `run_id`, `version`, `payload` JSON, `created_by`, `created_at`; PK(`run_id`, `version`) | только INSERT |
| `priority_configs` | то же | только INSERT |

Файлы документов лежат в `/data/files/<sha256>`. Индексы: `analysis_runs(doc_id)`,
`review_events(run_id, entity_id)`, `llm_calls(run_id)`.
