"""SQLite schema (data-model §10), engine with WAL and a single serialized writer."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    event,
    make_url,
    select,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

SCHEMA_VERSION = "1"

metadata = MetaData()

meta = Table(
    "meta",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=False),
)
documents = Table(
    "documents",
    metadata,
    Column("id", String, primary_key=True),
    Column("sha256", String(64), nullable=False, unique=True),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
fragments = Table(
    "fragments",
    metadata,
    Column("doc_id", String, nullable=False),
    Column("id", String, nullable=False),
    Column("order", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    PrimaryKeyConstraint("doc_id", "id"),
)
document_texts = Table(
    "document_texts",
    metadata,
    Column("doc_id", String, primary_key=True),
    Column("full_text", Text, nullable=False),
    Column("page_offsets", JSON, nullable=False),
)
analysis_runs = Table(
    "analysis_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("doc_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    Index("ix_analysis_runs_doc_id", "doc_id"),
)
stage_outputs = Table(
    "stage_outputs",
    metadata,
    Column("run_id", String, nullable=False),
    Column("stage", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("run_id", "stage"),
)
# Results of one item of a stage (a process in S4): kept so a resumed run redoes only the rest.
stage_items = Table(
    "stage_items",
    metadata,
    Column("run_id", String, nullable=False),
    Column("stage", String, nullable=False),
    Column("item_key", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("run_id", "stage", "item_key"),
)
llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, nullable=True),
    Column("stage", String, nullable=True),
    Column("model", String, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("status", String, nullable=False),
    Column("in_tokens", Integer, nullable=False),
    Column("out_tokens", Integer, nullable=False),
    Column("tokens_estimated", Integer, nullable=False, default=0),
    Column("latency_ms", Integer, nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("response", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_llm_calls_run_id", "run_id"),
)
llm_cache = Table(
    "llm_cache",
    metadata,
    Column("request_hash", String(64), primary_key=True),
    Column("response", JSON, nullable=False),
    Column("model", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
review_events = Table(
    "review_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, nullable=False),
    Column("entity_type", String, nullable=False),
    Column("entity_id", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    Index("ix_review_events_run_entity", "run_id", "entity_id"),
)


def _versioned_table(name: str) -> Table:
    return Table(
        name,
        metadata,
        Column("run_id", String, nullable=False),
        Column("version", Integer, nullable=False),
        Column("payload", JSON, nullable=False),
        Column("created_by", String, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        PrimaryKeyConstraint("run_id", "version"),
    )


assumption_sets = _versioned_table("assumption_sets")
priority_configs = _versioned_table("priority_configs")


class SchemaVersionError(RuntimeError):
    pass


class Database:
    """Async engine plus a lock that serializes every write transaction.

    SQLite allows one writer at a time; with WAL readers never block, and funnelling writes
    through one lock in this process means `database is locked` cannot come from our own
    concurrency. `busy_timeout` covers anything outside the process (backup tools, CLI).
    """

    def __init__(self, url: str, *, busy_timeout_ms: int = 5000) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url)
        self._write_lock = asyncio.Lock()
        if self.engine.dialect.name == "sqlite":
            self._install_sqlite_pragmas(busy_timeout_ms)

    def _install_sqlite_pragmas(self, busy_timeout_ms: int) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def _on_connect(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def init(self) -> None:
        """Create missing tables and refuse a database written by another schema version."""
        url = make_url(self.url)
        if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        async with self.write() as conn:
            await conn.run_sync(metadata.create_all)
            stored = await conn.scalar(select(meta.c.value).where(meta.c.key == "schema_version"))
            if stored is None:
                await conn.execute(meta.insert().values(key="schema_version", value=SCHEMA_VERSION))
            elif stored != SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema {stored}, code expects {SCHEMA_VERSION}; "
                    "migrate or point DATABASE_URL to a new file"
                )

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.connect() as conn:
            yield conn

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncConnection]:
        async with self._write_lock, self.engine.begin() as conn:
            yield conn

    async def dispose(self) -> None:
        await self.engine.dispose()
