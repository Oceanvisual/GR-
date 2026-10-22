"""Storage over a temporary SQLite file (T009)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from gr.domain.assumptions import Assumption, AssumptionSet, Triple
from gr.domain.document import Document, Fragment, IngestionDiagnostics
from gr.domain.enums import (
    AssumptionSource,
    DocFormat,
    FragmentKind,
    RunStatus,
    StageName,
    StructureMode,
    TextLayerStatus,
)
from gr.domain.ids import new_id
from gr.domain.priority import PriorityConfig, PriorityConfigVersion
from gr.domain.review import ReviewEvent
from gr.domain.run import AnalysisRun, ErrorInfo
from gr.storage import db as tables
from gr.storage.db import Database, SchemaVersionError
from gr.storage.files import FileStore
from gr.storage.repositories import (
    AssumptionSetRepository,
    ConflictError,
    DocumentText,
    LlmCall,
    NotFoundError,
    PriorityConfigRepository,
    Repositories,
    ReviewEventRepository,
)

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.fixture
async def repos(tmp_path: Path) -> AsyncIterator[Repositories]:
    database = Database(_url(tmp_path / "data" / "gr.db"))
    await database.init()
    yield Repositories(database)
    await database.dispose()


def _document(sha: str = "a" * 64) -> Document:
    return Document(
        id=new_id("doc"),
        filename="reg.docx",
        format=DocFormat.DOCX,
        size_bytes=10,
        sha256=sha,
        uploaded_at=NOW,
        uploaded_by="expert",
        char_count=12,
        diagnostics=IngestionDiagnostics(text_layer=TextLayerStatus.OK),
        structure_mode=StructureMode.NUMBERED,
    )


def _fragment(doc_id: str, fid: str, order: int) -> Fragment:
    return Fragment(
        id=fid,
        doc_id=doc_id,
        kind=FragmentKind.POINT,
        label=f"п. {order}",
        text="Текст.",
        start=order * 10,
        end=order * 10 + 6,
        order=order,
    )


def _run(doc_id: str, created_at: datetime = NOW) -> AnalysisRun:
    return AnalysisRun(id=new_id("run"), doc_id=doc_id, created_by="expert", created_at=created_at)


def _review(run_id: str, entity_id: str = "proc_1", n: int = 0) -> ReviewEvent:
    return ReviewEvent(
        id=new_id("rev"),
        run_id=run_id,
        entity_type="process",
        entity_id=entity_id,
        action="confirm",
        user="expert",
        created_at=NOW + timedelta(seconds=n),
    )


def _call(run_id: str, n: int = 0, *, estimated: bool = False) -> LlmCall:
    return LlmCall(
        id=new_id("llm"),
        run_id=run_id,
        stage="discover",
        model="fake",
        attempt=1,
        status="ok",
        in_tokens=100,
        out_tokens=20 + n,
        tokens_estimated=estimated,
        latency_ms=5,
        request_hash="f" * 64,
        response={"raw": n},
        created_at=NOW,
    )


def _assumptions(run_id: str, version: int) -> AssumptionSet:
    item = Assumption(
        key="hourly_cost",
        scope="global",
        unit="₽/ч",
        values=Triple(conservative=Decimal(1200), base=Decimal(1200), optimistic=Decimal(1200)),
        source=AssumptionSource.DEFAULT_PROFILE,
        updated_by="system",
        updated_at=NOW,
    )
    return AssumptionSet(
        run_id=run_id, version=version, items=[item], created_by="expert", created_at=NOW
    )


# --- schema --------------------------------------------------------------------------------


async def test_wal_and_busy_timeout(repos: Repositories) -> None:
    async with repos.db.read() as conn:
        assert (await conn.scalar(text("PRAGMA journal_mode"))) == "wal"
        assert (await conn.scalar(text("PRAGMA busy_timeout"))) == 5000


async def test_schema_version_mismatch_refused(tmp_path: Path) -> None:
    database = Database(_url(tmp_path / "gr.db"))
    await database.init()
    async with database.write() as conn:
        await conn.execute(tables.meta.update().values(value="0"))
    await database.dispose()
    reopened = Database(_url(tmp_path / "gr.db"))
    with pytest.raises(SchemaVersionError):
        await reopened.init()
    await reopened.dispose()


async def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "gr.db"
    first = Database(_url(path))
    await first.init()
    document = _document()
    await Repositories(first).documents.add(document)
    await first.dispose()

    second = Database(_url(path))
    await second.init()
    assert await Repositories(second).documents.get(document.id) == document
    await second.dispose()


# --- documents, fragments, texts -----------------------------------------------------------


async def test_documents_crud(repos: Repositories) -> None:
    document = _document()
    await repos.documents.add(document)
    assert await repos.documents.get(document.id) == document
    assert await repos.documents.find_by_sha256("a" * 64) == document
    assert await repos.documents.find_by_sha256("b" * 64) is None
    with pytest.raises(ConflictError):
        await repos.documents.add(_document())  # same sha256

    edited = document.model_copy(update={"relevance_note": "актуальная редакция"})
    await repos.documents.update(edited)
    assert (await repos.documents.get(document.id)).relevance_note == "актуальная редакция"
    assert [d.id for d in await repos.documents.list()] == [document.id]
    with pytest.raises(NotFoundError):
        await repos.documents.get("doc_missing")
    with pytest.raises(NotFoundError):
        await repos.documents.update(_document("c" * 64))


async def test_fragments_replaced_as_a_whole(repos: Repositories) -> None:
    doc_id = "doc_1"
    await repos.fragments.replace_for_document(
        doc_id, [_fragment(doc_id, "body/2", 2), _fragment(doc_id, "body/1", 1)]
    )
    assert [f.id for f in await repos.fragments.list_for_document(doc_id)] == ["body/1", "body/2"]
    await repos.fragments.replace_for_document(doc_id, [_fragment(doc_id, "body/9", 0)])
    assert [f.id for f in await repos.fragments.list_for_document(doc_id)] == ["body/9"]
    assert (await repos.fragments.get(doc_id, "body/9")).order == 0
    with pytest.raises(NotFoundError):
        await repos.fragments.get(doc_id, "body/1")
    with pytest.raises(ValueError, match="belong"):
        await repos.fragments.replace_for_document("doc_2", [_fragment(doc_id, "x", 0)])


async def test_document_text(repos: Repositories) -> None:
    await repos.texts.put(DocumentText(doc_id="doc_1", full_text="абв", page_offsets=[0]))
    await repos.texts.put(DocumentText(doc_id="doc_1", full_text="где", page_offsets=[0, 2]))
    stored = await repos.texts.get("doc_1")
    assert (stored.full_text, stored.page_offsets) == ("где", [0, 2])
    with pytest.raises(NotFoundError):
        await repos.texts.get("doc_2")


# --- runs and stage outputs ----------------------------------------------------------------


async def test_runs_crud_and_queries(repos: Repositories) -> None:
    older = _run("doc_1", NOW)
    newer = _run("doc_1", NOW + timedelta(minutes=1))
    other = _run("doc_2")
    for run in (older, newer, other):
        await repos.runs.add(run)
    with pytest.raises(ConflictError):
        await repos.runs.add(older)

    assert [r.id for r in await repos.runs.list_for_document("doc_1")] == [newer.id, older.id]
    running = older.transition(RunStatus.RUNNING)
    await repos.runs.update(running)
    assert (await repos.runs.get(older.id)).status == RunStatus.RUNNING
    assert [r.id for r in await repos.runs.list_by_status([RunStatus.RUNNING])] == [older.id]

    failed = running.transition(RunStatus.FAILED, error=ErrorInfo(code="x", message="y"))
    await repos.runs.update(failed)
    assert (await repos.runs.active_for_document("doc_1")) == newer
    assert await repos.runs.active_for_document("doc_3") is None


async def test_regenerations_are_runs_of_their_analysis(repos: Repositories) -> None:
    analysis = _run("doc_1", NOW)
    children = [
        AnalysisRun(id=new_id("run"), doc_id="doc_1", parent_run_id=analysis.id,
                    process_id="proc_1", created_by="expert", created_at=NOW + timedelta(minutes=n))
        for n in (2, 1)
    ]  # fmt: skip
    other = AnalysisRun(id=new_id("run"), doc_id="doc_1", parent_run_id=new_id("run"),
                        process_id="proc_1", created_by="expert", created_at=NOW)  # fmt: skip
    for run in (analysis, *children, other):
        await repos.runs.add(run)
    assert [r.id for r in await repos.runs.list_for_document("doc_1")] == [analysis.id]
    assert (await repos.runs.latest_by_document())["doc_1"].id == analysis.id
    assert [r.id for r in await repos.runs.regenerations(analysis)] == [
        children[1].id,
        children[0].id,
    ]  # oldest first
    assert (await repos.runs.active_for_document("doc_1")) is not None  # one at a time


async def test_stage_outputs_written_once(repos: Repositories) -> None:
    await repos.stage_outputs.put("run_1", StageName.DISCOVER, {"processes": [1]})
    assert await repos.stage_outputs.get("run_1", StageName.DISCOVER) == {"processes": [1]}
    assert await repos.stage_outputs.get("run_1", StageName.DETAIL) is None
    with pytest.raises(ConflictError):
        await repos.stage_outputs.put("run_1", StageName.DISCOVER, {"processes": [2]})
    assert await repos.stage_outputs.stages_done("run_1") == {StageName.DISCOVER}


# --- llm calls and cache -------------------------------------------------------------------


async def test_llm_calls_and_usage(repos: Repositories) -> None:
    await repos.llm_calls.add(_call("run_1", 0))
    await repos.llm_calls.add(_call("run_1", 1, estimated=True))
    await repos.llm_calls.add(_call("run_2", 5))
    calls = await repos.llm_calls.list_for_run("run_1")
    assert [c.response for c in calls] == [{"raw": 0}, {"raw": 1}]
    usage = await repos.llm_calls.usage_for_run("run_1")
    assert (usage.calls, usage.in_tokens, usage.out_tokens, usage.estimated) == (2, 200, 41, True)
    empty = await repos.llm_calls.usage_for_run("run_none")
    assert (empty.calls, empty.in_tokens, empty.estimated) == (0, 0, False)


async def test_llm_cache_first_writer_wins(repos: Repositories) -> None:
    assert await repos.llm_cache.get("h" * 64) is None
    await repos.llm_cache.put("h" * 64, "fake", {"answer": 1})
    await repos.llm_cache.put("h" * 64, "fake", {"answer": 2})
    assert await repos.llm_cache.get("h" * 64) == {"answer": 1}


# --- append-only tables --------------------------------------------------------------------


@pytest.mark.parametrize(
    "repository", [ReviewEventRepository, AssumptionSetRepository, PriorityConfigRepository]
)
def test_append_only_repositories_have_no_mutation(repository: type) -> None:
    public = {name for name in dir(repository) if not name.startswith("_")}
    assert not public & {"update", "delete", "put", "replace", "remove", "upsert"}


async def test_review_events(repos: Repositories) -> None:
    first, second = _review("run_1", "proc_1", 0), _review("run_1", "proc_2", 1)
    await repos.reviews.add(second)
    await repos.reviews.add(first)
    await repos.reviews.add(_review("run_2"))
    assert await repos.reviews.list_for_run("run_1") == [first, second]
    assert await repos.reviews.list_for_run("run_1", "proc_2") == [second]
    with pytest.raises(ConflictError):
        await repos.reviews.add(first)


async def test_versioned_sets(repos: Repositories) -> None:
    assert await repos.assumptions.latest("run_1") is None
    await repos.assumptions.add(_assumptions("run_1", 1))
    await repos.assumptions.add(_assumptions("run_1", 2))
    with pytest.raises(ConflictError, match="does not follow"):
        await repos.assumptions.add(_assumptions("run_1", 2))
    with pytest.raises(ConflictError, match="does not follow"):
        await repos.assumptions.add(_assumptions("run_1", 4))
    latest = await repos.assumptions.latest("run_1")
    assert latest is not None and latest.version == 2
    assert (await repos.assumptions.get("run_1", 1)).version == 1
    assert await repos.assumptions.versions("run_1") == [1, 2]
    with pytest.raises(NotFoundError):
        await repos.assumptions.get("run_1", 3)

    config = PriorityConfigVersion(
        run_id="run_1", version=1, config=PriorityConfig(), created_by="expert", created_at=NOW
    )
    await repos.priority_configs.add(config)
    assert await repos.priority_configs.latest("run_1") == config


async def test_concurrent_version_saves_one_wins(repos: Repositories) -> None:
    results = await asyncio.gather(
        *(repos.assumptions.add(_assumptions("run_1", 1)) for _ in range(5)),
        return_exceptions=True,
    )
    assert sum(r is None for r in results) == 1
    assert all(isinstance(r, ConflictError) for r in results if r is not None)


# --- concurrency ---------------------------------------------------------------------------


async def test_concurrent_writers_do_not_lock(repos: Repositories) -> None:
    """Runner, API and OCR write at once: 50 mixed writes plus reads, no `database is locked`."""
    runs = [_run(f"doc_{i}") for i in range(10)]
    for run in runs:
        await repos.runs.add(run)

    async def runner(i: int) -> None:
        run = runs[i % len(runs)]
        await repos.llm_calls.add(_call(run.id, i))
        await repos.runs.update(run.model_copy(update={"usage": run.usage}))

    async def api(i: int) -> None:
        await repos.reviews.add(_review(runs[i % len(runs)].id, f"proc_{i}", i))
        await repos.runs.list_for_document(f"doc_{i % 10}")

    async def ocr(i: int) -> None:
        doc_id = f"doc_ocr_{i}"
        await repos.texts.put(DocumentText(doc_id=doc_id, full_text="т" * 1000))
        await repos.fragments.replace_for_document(
            doc_id, [_fragment(doc_id, f"body/{k}", k) for k in range(20)]
        )

    jobs = [(runner, api, ocr)[i % 3](i) for i in range(50)]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors
    stored = [len(await repos.llm_calls.list_for_run(r.id)) for r in runs]
    assert sum(stored) == 17


# --- files ---------------------------------------------------------------------------------


async def test_file_store_atomic_and_idempotent(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "files")
    digest = await store.put(b"content")
    assert await store.put(b"content") == digest
    assert await store.read(digest) == b"content"
    assert store.exists(digest)
    assert [p.name for p in (tmp_path / "files").iterdir()] == [digest]


@pytest.mark.parametrize("name", ["../../etc/passwd", "A" * 64, "abc"])
def test_file_store_rejects_non_digest(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="sha256"):
        FileStore(tmp_path).path_for(name)


async def test_stage_items_are_per_run_stage_and_written_once(repos: Repositories) -> None:
    detail = repos.stage_items.for_stage("run_1", StageName.DETAIL)
    await detail.put("proc_1", {"windows": 1})
    assert await detail.get("proc_1") == {"windows": 1}
    assert await detail.get("proc_2") is None
    assert await repos.stage_items.for_stage("run_2", StageName.DETAIL).get("proc_1") is None
    assert await repos.stage_items.for_stage("run_1", StageName.DISCOVER).get("proc_1") is None
    with pytest.raises(ConflictError):
        await detail.put("proc_1", {"windows": 2})
