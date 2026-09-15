"""Async SQLite engine, PRAGMAs, and schema bootstrap.

The PRAGMAs are not boilerplate — each one is load-bearing for the "one daemon,
many concurrent lanes" access pattern:

``journal_mode=WAL``     readers never block the writer, which is what lets the
                         TUI tail the bus log while lanes are writing to it.
``busy_timeout``         a lane writing during a checkpoint waits instead of
                         immediately raising "database is locked".
``foreign_keys=ON``      SQLite defaults this off; we want referential mistakes
                         to be loud.
``synchronous=NORMAL``   the right durability/speed trade for WAL: safe against
                         process crash, which is the failure mode we actually
                         recover from.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from openburrow.core.db.models import ALL_TABLES, SchemaVersionRow
from openburrow.core.errors import BusError
from openburrow.core.logging import get_logger
from openburrow.core.models.base import now
from openburrow.core.version import SCHEMA_VERSION

log = get_logger(__name__)


def create_engine(
    db_url: str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    busy_timeout_ms: int = 5000,
    journal_mode: str = "WAL",
) -> AsyncEngine:
    """Build an async engine with the SQLite PRAGMAs applied.

    Non-SQLite URLs (a future Postgres-backed daemon) skip the SQLite-specific
    pragmas rather than erroring, so this function stays the single entry point.
    """
    is_sqlite = db_url.startswith("sqlite")

    connect_args: dict[str, object] = {}
    if is_sqlite:
        connect_args["timeout"] = busy_timeout_ms / 1000
        connect_args["check_same_thread"] = False

    engine = create_async_engine(
        db_url,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
        **({} if is_sqlite else {"pool_size": pool_size, "max_overflow": pool_size * 2}),
    )

    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA journal_mode={journal_mode}")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.execute("PRAGMA cache_size=-32000")  # ~32 MB page cache
            finally:
                cursor.close()

    return engine


class Database:
    """Owns the engine and the session factory for one repo's local state."""

    def __init__(self, url: str, *, echo: bool = False, **engine_kwargs: object) -> None:
        self.url = url
        self.engine: AsyncEngine = create_engine(url, echo=echo, **engine_kwargs)  # type: ignore[arg-type]
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    # --- lifecycle ---------------------------------------------------------
    async def create_all(self) -> None:
        """Create any missing tables. Safe to call on every daemon start."""
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        await self._stamp_schema_version()

    async def _stamp_schema_version(self) -> None:
        async with self.session_factory() as session:
            existing = await session.get(SchemaVersionRow, SCHEMA_VERSION)
            if existing is None:
                session.add(
                    SchemaVersionRow(
                        version=SCHEMA_VERSION,
                        description=f"baseline schema v{SCHEMA_VERSION}",
                    )
                )
                await session.commit()

    async def schema_version(self) -> int:
        async with self.session_factory() as session:
            result = await session.execute(text("SELECT MAX(version) FROM schema_version"))
            value = result.scalar()
            return int(value or 0)

    async def healthcheck(self) -> dict[str, object]:
        """Round-trip a trivial query and report size — used by ``burrow doctor``."""
        try:
            async with self.session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            return {"ok": False, "url": self.url, "error": str(exc)}

        size_bytes: int | None = None
        path = self.sqlite_path()
        if path is not None and path.exists():
            size_bytes = path.stat().st_size

        return {
            "ok": True,
            "url": self.url,
            "schema_version": await self.schema_version(),
            "path": str(path) if path else None,
            "size_bytes": size_bytes,
            "journal_mode": await self.journal_mode(),
        }

    async def journal_mode(self) -> str:
        async with self.session_factory() as session:
            result = await session.execute(text("PRAGMA journal_mode"))
            return str(result.scalar() or "")

    def sqlite_path(self) -> Path | None:
        """Filesystem path of the SQLite file, or ``None`` for memory/remote."""
        if not self.url.startswith("sqlite"):
            return None
        raw = self.url.split("///", 1)[-1]
        if not raw or raw == ":memory:" or "mode=memory" in raw:
            return None
        return Path(raw)

    async def close(self) -> None:
        await self.engine.dispose()

    # --- sessions ----------------------------------------------------------
    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional scope: commits on success, rolls back on exception."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def vacuum(self) -> None:
        """Reclaim space after retention pruning. Requires no concurrent writers."""
        async with self.engine.begin() as connection:
            await connection.execute(text("VACUUM"))

    async def backup(self, destination: Path | str) -> Path:
        """Copy the database file. Callers should stop writes first."""
        source = self.sqlite_path()
        if source is None:
            raise BusError("cannot back up a non-file database", context={"url": self.url})
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copy2(source, target)
        log.info("db.backup.written", source=str(source), target=str(target))
        return target


# ---------------------------------------------------------------------------
# Process-wide registry
# ---------------------------------------------------------------------------
_databases: dict[str, Database] = {}


def get_database(url: str, *, echo: bool = False, **kwargs: object) -> Database:
    """Return a cached :class:`Database` per URL.

    Caching matters because SQLite WAL does not enjoy many engines pointed at
    the same file within one process; one engine per URL is the supported shape.
    """
    if url not in _databases:
        _databases[url] = Database(url, echo=echo, **kwargs)
    return _databases[url]


async def init_database(url: str, *, echo: bool = False) -> Database:
    """Get-or-create the database and ensure the schema exists."""
    database = get_database(url, echo=echo)
    await database.create_all()
    return database


@asynccontextmanager
async def session_scope(url: str) -> AsyncIterator[AsyncSession]:
    """Convenience: ``async with session_scope(url) as session: ...``"""
    database = get_database(url)
    async with database.session() as session:
        yield session


async def close_all() -> None:
    """Dispose every cached engine. Called on daemon shutdown."""
    for database in list(_databases.values()):
        await database.close()
    _databases.clear()


def default_db_url(repo_root: Path | str) -> str:
    """Compute the canonical SQLite URL for a repo, honoring ``OPENBURROW_STATE_DIR``."""
    override = os.environ.get("OPENBURROW_STATE_DIR", "").strip()
    if override:
        base = Path(override).expanduser().resolve()
    else:
        base = Path(repo_root).expanduser().resolve() / ".openburrow"
    return f"sqlite+aiosqlite:///{(base / 'burrow.db').as_posix()}"


__all__ = [
    "ALL_TABLES",
    "Database",
    "close_all",
    "create_engine",
    "default_db_url",
    "get_database",
    "init_database",
    "now",
    "session_scope",
]
