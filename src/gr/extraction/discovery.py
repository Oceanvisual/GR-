"""S3 discover: the map of administrative processes (contracts/llm.md § S3, R-10).

One LLM call per outline chunk; then deterministic work: aliases back to fragment ids
(unknown ones dropped with an issue), candidates without action fragments dropped, evidence
quotes verified (R-11), candidates from different chunks merged when their action fragments
overlap (Jaccard ≥ 0.5). An empty map is a valid result: «процессы не найдены».
"""

import asyncio
import re
from collections.abc import Iterable, Iterator, Sequence
from typing import Literal

from pydantic import BaseModel

from gr.domain.document import Citation, DocumentMetadata, Fragment
from gr.domain.enums import Confidence, GroundingStatus, StageName
from gr.extraction.mapping import FactMapper
from gr.extraction.outline import OutlineChunk, build_outline
from gr.extraction.prompt_files import load_prompt
from gr.extraction.render import ChunkingConfig, render_document
from gr.extraction.schemas import DiscoveryOut, ProcessCandidate
from gr.llm.base import StructuredLLM

# A list of administrative procedures, each with its fragments, plus the non-process aliases:
# 4000 cut the answer on regulations of ~15 procedures (T059).
MAX_TOKENS = 8000
MERGE_JACCARD = 0.5
# A channel in a process name does not make another procedure (T059): names equal without
# it are one process — «Рассмотрение документов в электронной форме», «… (личный прием)».
_CHANNEL_RE = re.compile(
    r"\([^)]*\)|\bв\s+электронн\w+\s+(?:форме|виде)\b"
    r"|\b(?:через|посредством|с\s+использованием)\s+(?:\w+\s+){0,3}"
    r"(?:портал\w*|многофункциональн\w+\s+центр\w*|мфц)\b",
    re.IGNORECASE,
)

IssueCode = Literal[
    "unknown_fragment", "empty_core", "unverified_evidence", "duplicate_merged", "no_processes"
]


class DiscoveryIssue(BaseModel):
    code: IssueCode
    element: str  # process key or alias
    message: str


class DiscoveredProcess(BaseModel):
    key: str  # proc_1… in document order of the first action fragment
    name: str
    trigger: str = ""
    result: str = ""
    core_fragment_ids: list[str]
    related_fragment_ids: list[str] = []
    evidence: Citation | None = None
    confidence: Confidence
    parent_key: str | None = None


class DiscoveryResult(BaseModel):
    processes: list[DiscoveredProcess]
    non_process_fragment_ids: list[str] = []
    issues: list[DiscoveryIssue] = []
    notes: list[str] = []
    chunks: int
    prompt_version: str


class _Candidate(BaseModel):
    """A process from one chunk, ids resolved, before merging."""

    key: str  # chunk-qualified: "c0.p1"
    name: str
    trigger: str
    result: str
    core: list[str]
    related: list[str]
    evidence: Citation | None
    parent: str | None


def _user_message(task: str, chunk: OutlineChunk, number: int, total: int, meta: str) -> str:
    part = f" Это часть {number} из {total} оглавления." if total > 1 else ""
    return f"<task>{task}{part}</task>\n" + render_document(chunk.text, {"title": meta})


class _Mapper:
    def __init__(self, fragments: Sequence[Fragment]) -> None:
        self.facts = FactMapper(fragments)
        self.aliases = self.facts.aliases
        self.order = {f.id: f.order for f in fragments}
        self.parent = {f.id: f.parent_id for f in fragments}
        self.issues: list[DiscoveryIssue] = []

    def topmost(self, ids: list[str], covered: Iterable[str] = ()) -> list[str]:
        """Drop fragments already inside another listed (or covered) one: a reference takes
        the whole subtree, so «п. 3.2» and «абз. 2 п. 3.2» say the same as «п. 3.2»."""
        above = set(ids) | set(covered)
        kept = []
        for fragment_id in ids:
            parent = self.parent.get(fragment_id)
            while parent is not None and parent not in above:
                parent = self.parent.get(parent)
            if parent is None:
                kept.append(fragment_id)
        return kept

    def ancestors(self, fragment_id: str) -> Iterator[str]:
        parent = self.parent.get(fragment_id)
        while parent is not None:
            yield parent
            parent = self.parent.get(parent)

    def ids(self, refs: list[str], element: str) -> list[str]:
        resolved: list[str] = []
        for ref in refs:
            fragment_id = self.aliases.resolve(ref)
            if fragment_id is None:
                self.issues.append(
                    DiscoveryIssue(
                        code="unknown_fragment",
                        element=element,
                        message=f"Ссылка на несуществующий фрагмент «{ref[:20]}» отброшена.",
                    )
                )
            elif fragment_id not in resolved:
                resolved.append(fragment_id)
        return sorted(resolved, key=self.order.__getitem__)

    def evidence(self, item: ProcessCandidate) -> Citation | None:
        if not item.evidence.quote.strip():
            return None
        return self.facts.check(item.evidence).citation

    def candidate(self, item: ProcessCandidate, chunk: int) -> _Candidate | None:
        core = self.topmost(self.ids(item.core_refs, item.key))
        if not core:
            self.issues.append(
                DiscoveryIssue(
                    code="empty_core",
                    element=item.key,
                    message=f"Процесс «{item.name[:80]}» отброшен: нет фрагментов с действиями.",
                )
            )
            return None
        related = [i for i in self.ids(item.related_refs, item.key) if i not in core]
        return _Candidate(
            key=f"c{chunk}.{item.key}",
            name=item.name.strip(),
            trigger=item.trigger.strip(),
            result=item.result.strip(),
            core=core,
            related=related,
            evidence=self.evidence(item),
            parent=f"c{chunk}.{item.parent_key}" if item.parent_key.strip() else None,
        )


def _jaccard(a: list[str], b: list[str]) -> float:
    left, right = set(a), set(b)
    return len(left & right) / len(left | right) if left | right else 0.0


def _passed(citation: Citation | None) -> bool:
    return citation is not None and citation.grounding.status.passes


def _confidence(citation: Citation | None) -> Confidence:
    """R-11: an exact verified quote — high; fuzzy or relocated — medium; failed — low."""
    if citation is None or not citation.grounding.status.passes:
        return Confidence.LOW
    exact = (
        citation.grounding.status == GroundingStatus.VERIFIED and citation.grounding.score == 100
    )
    return Confidence.HIGH if exact else Confidence.MEDIUM


def bare_name(name: str) -> str:
    """The name of a procedure without its channel, for comparison only."""
    text = _CHANNEL_RE.sub(" ", name.lower().replace("ё", "е"))
    return " ".join(re.findall(r"\w+", text))


def _same(a: "_Candidate", b: "_Candidate") -> bool:
    if _jaccard(a.core, b.core) >= MERGE_JACCARD:
        return True
    name = bare_name(a.name)
    return bool(name) and name == bare_name(b.name)


def _merge(
    candidates: list[_Candidate], issues: list[DiscoveryIssue]
) -> tuple[list[_Candidate], dict[str, str]]:
    """Greedy merge in chunk order; returns the merged list and old key → surviving key."""
    merged: list[_Candidate] = []
    alias: dict[str, str] = {}
    for candidate in candidates:
        overlapping = (m for m in merged if _same(m, candidate))
        target = next(overlapping, None)
        if target is None:
            merged.append(candidate)
            alias[candidate.key] = candidate.key
            continue
        alias[candidate.key] = target.key
        issues.append(
            DiscoveryIssue(
                code="duplicate_merged",
                element=candidate.key,
                message=f"«{candidate.name[:80]}» объединён с «{target.name[:80]}».",
            )
        )
        index = merged.index(target)
        merged[index] = target.model_copy(
            update={
                "core": list(dict.fromkeys([*target.core, *candidate.core])),
                "related": list(dict.fromkeys([*target.related, *candidate.related])),
                "trigger": target.trigger or candidate.trigger,
                "result": target.result or candidate.result,
                "evidence": target.evidence
                if _passed(target.evidence) or not _passed(candidate.evidence)
                else candidate.evidence,
                "parent": target.parent or candidate.parent,
            }
        )
    return merged, alias


async def discover(
    fragments: Sequence[Fragment],
    metadata: DocumentMetadata,
    llm: StructuredLLM,
    *,
    run_id: str,
    chunking: ChunkingConfig | None = None,
) -> DiscoveryResult:
    prompt = load_prompt("discover")
    mapper = _Mapper(fragments)
    chunks = build_outline(fragments, mapper.aliases, chunking)
    title = metadata.title.value or ""
    answers = await asyncio.gather(
        *(
            llm.generate(
                schema=DiscoveryOut,
                system=prompt.system,
                user=_user_message(prompt.task, chunk, n, len(chunks), title),
                stage=StageName.DISCOVER,
                max_tokens=MAX_TOKENS,
                run_id=run_id,
            )
            for n, chunk in enumerate(chunks, 1)
        )
    )

    candidates: list[_Candidate] = []
    non_process: list[str] = []
    notes: list[str] = []
    for n, answer in enumerate(answers):
        out = answer.value
        for item in out.processes:
            candidate = mapper.candidate(item, n)
            if candidate is not None:
                candidates.append(candidate)
        non_process += mapper.ids(out.non_process_refs, f"c{n}")
        if out.notes.strip():
            notes.append(out.notes.strip())

    merged, alias = _merge(candidates, mapper.issues)
    merged.sort(key=lambda c: mapper.order[c.core[0]])
    final_key = {c.key: f"proc_{i}" for i, c in enumerate(merged, 1)}
    processes: list[DiscoveredProcess] = []
    for candidate in merged:
        key = final_key[candidate.key]
        parent = final_key.get(alias.get(candidate.parent or "", ""))
        core = mapper.topmost(sorted(candidate.core, key=mapper.order.__getitem__))
        if not _passed(candidate.evidence):
            mapper.issues.append(
                DiscoveryIssue(
                    code="unverified_evidence",
                    element=key,
                    message=f"Цитата-подтверждение процесса «{candidate.name[:80]}» не найдена.",
                )
            )
        processes.append(
            DiscoveredProcess(
                key=key,
                name=candidate.name,
                trigger=candidate.trigger,
                result=candidate.result,
                core_fragment_ids=core,
                related_fragment_ids=mapper.topmost(
                    [i for i in candidate.related if i not in core], covered=core
                ),
                evidence=candidate.evidence,
                confidence=_confidence(candidate.evidence),
                parent_key=parent if parent != key else None,
            )
        )
    if not processes:
        mapper.issues.append(
            DiscoveryIssue(
                code="no_processes",
                element="",
                message="Процессы не найдены: акт не описывает административных процедур.",
            )
        )
    used = {i for p in processes for i in (*p.core_fragment_ids, *p.related_fragment_ids)}
    # A non-process fragment takes its subtree (coverage, FR-028): one that holds a process —
    # «body» was answered on reg261 — would hide every gap in the act. Only action fragments
    # count: a related mention is context and does not overturn the verdict for its section.
    holding = {a for p in processes for i in p.core_fragment_ids for a in mapper.ancestors(i)}
    outside = sorted(
        {i for i in non_process if i not in used and i not in holding},
        key=mapper.order.__getitem__,
    )
    return DiscoveryResult(
        processes=processes,
        non_process_fragment_ids=mapper.topmost(outside),
        issues=mapper.issues,
        notes=notes,
        chunks=len(chunks),
        prompt_version=prompt.version,
    )
