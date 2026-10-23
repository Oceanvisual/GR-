"""S4 detail: one process at a time down to operations (contracts/llm.md § S4, R-10).

The text of a process is its action fragments (with subtrees) plus related ones (deadlines,
document lists, grounds for refusal). Above `detail.max_chars` the action fragments are cut
into consecutive windows, each carrying the process card and the related text. A window whose
answer is cut by the token limit is halved and redone, up to three times. Windows are merged
in document order.

Each finished process is stored at once, so a resumed run redoes only the rest. Failed
processes are reported; more than 30 % of them fail the stage. Mapping aliases to fragment
ids and verifying quotes is the validator's job (S5).
"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from pydantic import BaseModel

from gr.domain.document import DocumentMetadata, Fragment
from gr.domain.enums import StageName
from gr.extraction.discovery import DiscoveredProcess, DiscoveryResult
from gr.extraction.prompt_files import load_prompt
from gr.extraction.render import (
    ChunkingConfig,
    FragmentAliases,
    escape,
    load_chunking,
    render_document,
    render_fragments,
    with_subtrees,
)
from gr.extraction.schemas import DetailOut, OperationOut
from gr.llm.base import LLMError, LLMOutputTruncatedError, StructuredLLM
from gr.logging import get_logger
from gr.pipeline.stages import StageError

log = get_logger(__name__)

MAX_SPLIT_DEPTH = 3
FAILURE_SHARE = 0.3


class ItemStore(Protocol):
    async def get(self, item_key: str) -> dict[str, Any] | None: ...
    async def put(self, item_key: str, payload: dict[str, Any]) -> None: ...


class DetailedProcess(BaseModel):
    process_key: str
    windows: int
    detail: DetailOut


class DetailFailure(BaseModel):
    process_key: str
    name: str
    code: str
    message: str


class DetailingResult(BaseModel):
    processes: list[DetailedProcess]
    failed: list[DetailFailure] = []
    prompt_version: str


# --- merging windows -----------------------------------------------------------------------


def _prefixed(detail: DetailOut, prefix: str) -> DetailOut:
    """Window-local keys made unique: «o1» of window 2 becomes «w2.o1»."""
    keys = {op.key for op in detail.operations} | {dp.key for dp in detail.decision_points}

    def key(value: str) -> str:
        return f"{prefix}.{value}" if value in keys else value

    operations = [
        op.model_copy(update={"key": key(op.key), "depends_on": [key(d) for d in op.depends_on]})
        for op in detail.operations
    ]
    points = [
        dp.model_copy(
            update={
                "key": key(dp.key),
                "after_operation": key(dp.after_operation),
                "outcomes": [
                    o.model_copy(update={"next_operation": key(o.next_operation)})
                    if o.next_operation
                    else o
                    for o in dp.outcomes
                ],
            }
        )
        for dp in detail.decision_points
    ]
    return detail.model_copy(update={"operations": operations, "decision_points": points})


def _unique[T: BaseModel](items: list[T]) -> list[T]:
    seen: set[str] = set()
    kept: list[T] = []
    for item in items:
        fingerprint = item.model_dump_json()
        if fingerprint not in seen:
            seen.add(fingerprint)
            kept.append(item)
    return kept


def merge_windows(parts: Sequence[DetailOut]) -> DetailOut:
    """Windows in document order → one answer. The first operation of a window that names no
    predecessor continues the previous window."""
    if len(parts) == 1:
        return parts[0]
    windows = [_prefixed(part, f"w{n}") for n, part in enumerate(parts, 1)]
    operations: list[OperationOut] = []
    for window in windows:
        ops = list(window.operations)
        if operations and ops and not ops[0].depends_on:
            ops[0] = ops[0].model_copy(update={"depends_on": [operations[-1].key]})
        operations += ops
    first = windows[0].process

    def first_non_empty(field: str) -> Any:
        return next((getattr(w.process, field) for w in windows if getattr(w.process, field)), [])

    process = first.model_copy(
        update={
            "goal": first_non_empty("goal"),
            "trigger": first_non_empty("trigger"),
            "result": first_non_empty("result"),
            "participants": _unique([p for w in windows for p in w.process.participants]),
            "inputs": _unique([i for w in windows for i in w.process.inputs]),
            "information_systems": _unique(
                [s for w in windows for s in w.process.information_systems]
            ),
            "deadlines": _unique([d for w in windows for d in w.process.deadlines]),
        }
    )
    return DetailOut(
        process=process,
        operations=operations,
        decision_points=[dp for w in windows for dp in w.decision_points],
    )


# --- one process ---------------------------------------------------------------------------


class _ProcessText:
    """Rendered blocks of a process: the card, related context and action fragments."""

    def __init__(
        self,
        process: DiscoveredProcess,
        fragments: Sequence[Fragment],
        aliases: FragmentAliases,
        budget: int,
    ) -> None:
        self.fragments = fragments
        self.aliases = aliases
        self.core = [f for f in with_subtrees(fragments, process.core_fragment_ids) if f.text]
        core_ids = {f.id for f in self.core}
        related = with_subtrees(fragments, process.related_fragment_ids)
        self.related = [f for f in related if f.text and f.id not in core_ids]
        self.size = {f.id: len(self.render([f])) + 1 for f in [*self.core, *self.related]}
        self.card = _card(process)
        related_size = sum(self.size[f.id] for f in self.related)
        # Huge related text would leave no room for actions: then it goes with the first window.
        self.related_everywhere = related_size <= budget // 2
        self.budget = budget

    def render(self, blocks: Sequence[Fragment]) -> str:
        return render_fragments(blocks, self.fragments, self.aliases)

    def windows(self) -> list[list[Fragment]]:
        fixed = len(self.card) + (
            sum(self.size[f.id] for f in self.related) if self.related_everywhere else 0
        )
        windows: list[list[Fragment]] = []
        current: list[Fragment] = []
        size = fixed
        for block in self.core:
            if current and size + self.size[block.id] > self.budget:
                windows.append(current)
                current, size = [], fixed
            current.append(block)
            size += self.size[block.id]
        if current:
            windows.append(current)
        return windows or [[]]

    def document(self, core_part: list[Fragment], first: bool) -> str:
        context = self.related if first or self.related_everywhere else []
        blocks = sorted([*core_part, *context], key=lambda f: f.order)
        return self.render(blocks)


def _card(process: DiscoveredProcess) -> str:
    lines = [f"Процесс: «{escape(process.name)}»."]
    if process.trigger:
        lines.append(f"Запуск: {escape(process.trigger)}.")
    if process.result:
        lines.append(f"Результат: {escape(process.result)}.")
    return "\n".join(lines)


class Detailer:
    def __init__(
        self,
        fragments: Sequence[Fragment],
        metadata: DocumentMetadata,
        llm: StructuredLLM,
        *,
        run_id: str,
        max_tokens: int,
        chunking: ChunkingConfig | None = None,
    ) -> None:
        self.fragments = fragments
        self.llm = llm
        self.run_id = run_id
        self.max_tokens = max_tokens
        self.budget = (chunking or load_chunking()).detail_max_chars
        self.aliases = FragmentAliases(fragments)
        self.prompt = load_prompt("detail")
        self.title = metadata.title.value or ""

    async def process(self, process: DiscoveredProcess) -> DetailedProcess:
        text = _ProcessText(process, self.fragments, self.aliases, self.budget)
        windows = text.windows()
        parts: list[DetailOut] = []
        for n, core_part in enumerate(windows, 1):
            note = (
                f"\nЭто часть {n} из {len(windows)} текста процесса: извлекай операции только "
                "из этой части, остальные части обрабатываются отдельно."
                if len(windows) > 1
                else ""
            )
            parts += await self._window(text, core_part, n == 1, note, depth=0)
        return DetailedProcess(
            process_key=process.key, windows=len(parts), detail=merge_windows(parts)
        )

    async def _window(
        self, text: _ProcessText, core_part: list[Fragment], first: bool, note: str, depth: int
    ) -> list[DetailOut]:
        # The card is the model's own reading of the document, so it is data, not the task.
        user = (
            f"<task>{self.prompt.task}{note}</task>\n"
            f"<process_card>\n{text.card}\n</process_card>\n"
            + render_document(text.document(core_part, first), {"title": self.title})
        )
        try:
            answer = await self.llm.generate(
                schema=DetailOut,
                system=self.prompt.system,
                user=user,
                stage=StageName.DETAIL,
                max_tokens=self.max_tokens,
                run_id=self.run_id,
            )
            return [answer.value]
        except LLMOutputTruncatedError:
            if depth >= MAX_SPLIT_DEPTH or len(core_part) < 2:
                raise
            log.info("detail.window_split", depth=depth + 1, blocks=len(core_part))
            middle = len(core_part) // 2
            left = await self._window(text, core_part[:middle], first, note, depth + 1)
            right = await self._window(text, core_part[middle:], False, note, depth + 1)
            return [*left, *right]


async def detail_processes(
    discovery: DiscoveryResult,
    fragments: Sequence[Fragment],
    metadata: DocumentMetadata,
    llm: StructuredLLM,
    *,
    run_id: str,
    max_tokens: int,
    concurrency: int,
    store: ItemStore | None = None,
    chunking: ChunkingConfig | None = None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> DetailingResult:
    detailer = Detailer(
        fragments, metadata, llm, run_id=run_id, max_tokens=max_tokens, chunking=chunking
    )
    semaphore = asyncio.Semaphore(concurrency)
    total = len(discovery.processes)
    finished = 0

    async def report() -> None:
        nonlocal finished
        finished += 1
        if progress is not None:
            await progress(finished, total)

    async def one(process: DiscoveredProcess) -> DetailedProcess | DetailFailure:
        if store is not None and (saved := await store.get(process.key)) is not None:
            await report()
            return DetailedProcess.model_validate(saved)
        async with semaphore:
            try:
                detailed = await detailer.process(process)
            except (LLMError, StageError) as exc:
                log.warning("detail.process_failed", process=process.key, error_code=exc.code)
                await report()
                return DetailFailure(
                    process_key=process.key,
                    name=process.name,
                    code=exc.code,
                    message=exc.message_ru,
                )
        if store is not None:
            await store.put(process.key, detailed.model_dump(mode="json"))
        await report()
        return detailed

    outcomes = await asyncio.gather(*(one(p) for p in discovery.processes))
    done = [o for o in outcomes if isinstance(o, DetailedProcess)]
    failed = [o for o in outcomes if isinstance(o, DetailFailure)]
    if outcomes and len(failed) / len(outcomes) > FAILURE_SHARE:
        code = Counter(f.code for f in failed).most_common(1)[0][0]
        raise StageError(
            code,
            f"Не удалось разобрать {len(failed)} из {len(outcomes)} процессов: {failed[0].message}",
        )
    return DetailingResult(processes=done, failed=failed, prompt_version=detailer.prompt.version)
