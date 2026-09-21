"""The persistent DuckDB catalog: one view per table plus the runtime state of refreshes.

The catalog file (``paths.catalog_path``) changes only through :func:`carquery.refresh.refresh`.
Each view is pinned to the exact file list the last activated refresh scanned, so files that
land between refreshes stay invisible and the data version describes what queries see.

State tables (all names start with ``_``):

- ``_data_version``: one row per activated version; the highest ``version_id`` is active.
- ``_refresh_log``: one row per refresh attempt (``active``, ``rejected``, ``no_change``,
  ``error``).
- ``_table_stats``: per version and table, row/file counts, bytes and the date range.
- ``_files``: the file list behind the active views.

DuckDB allows many read-only processes or one writer per file. Readers use short-lived
connections from :func:`open_catalog`; writers use :func:`connect_catalog`, which retries for
``catalog.busy_timeout_seconds`` while another process holds the file.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb

from carquery.config import AppConfig
from carquery.datafiles import create_file_views
from carquery.logging import get_logger
from carquery.storage import FileInfo, get_connector

log = get_logger(__name__)

STATE_DDL = (
    """CREATE TABLE IF NOT EXISTS _data_version (
        version_id INTEGER PRIMARY KEY,
        label VARCHAR NOT NULL,
        fingerprint VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL,
        data_through DATE,
        validated BOOLEAN NOT NULL,
        connector VARCHAR NOT NULL,
        refresh_id VARCHAR NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS _refresh_log (
        refresh_id VARCHAR PRIMARY KEY,
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP NOT NULL,
        status VARCHAR NOT NULL,
        fingerprint VARCHAR,
        file_count INTEGER,
        issue_count INTEGER,
        version_id INTEGER,
        message VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS _table_stats (
        version_id INTEGER NOT NULL,
        table_name VARCHAR NOT NULL,
        row_count BIGINT,
        file_count INTEGER NOT NULL,
        bytes BIGINT NOT NULL,
        min_date DATE,
        max_date DATE,
        PRIMARY KEY (version_id, table_name)
    )""",
    """CREATE TABLE IF NOT EXISTS _files (
        table_name VARCHAR NOT NULL,
        uri VARCHAR NOT NULL,
        size BIGINT NOT NULL,
        modified TIMESTAMP NOT NULL
    )""",
)
STATE_TABLES = ("_data_version", "_refresh_log", "_table_stats", "_files")
_BUSY_RETRY_SECONDS = (0.05, 0.1, 0.25, 0.5, 1.0)
# How DuckDB reports a catalog in use: another process holds it (POSIX lock / Windows sharing
# violation), or this process has it open in the other mode (e.g. queries running during an
# in-process refresh).
_BUSY_MARKERS = (
    "could not set lock",
    "conflicting lock",
    "being used by another process",
    "different configuration than existing connections",
)


class CatalogError(Exception):
    """The catalog is missing, busy or unreadable."""


@dataclass(frozen=True)
class DataVersion:
    version_id: int
    label: str
    fingerprint: str
    created_at: datetime
    data_through: date | None
    validated: bool
    connector: str
    refresh_id: str


@dataclass(frozen=True)
class TableStats:
    table: str
    row_count: int | None
    file_count: int
    bytes: int
    min_date: date | None = None
    max_date: date | None = None


@dataclass(frozen=True)
class RefreshLogEntry:
    refresh_id: str
    started_at: datetime
    finished_at: datetime
    status: str
    fingerprint: str | None = None
    file_count: int | None = None
    issue_count: int | None = None
    version_id: int | None = None
    message: str | None = None


# --------------------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------------------


def connect_catalog(
    path: Path, read_only: bool = True, busy_timeout: float = 0.0
) -> duckdb.DuckDBPyConnection:
    """Open the catalog file, retrying up to ``busy_timeout`` seconds while it is locked."""
    if read_only and not path.is_file():
        raise CatalogError(f"No catalog at {path}; run `carq refresh` first")
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + busy_timeout
    attempt = 0
    while True:
        try:
            return duckdb.connect(str(path), read_only=read_only)
        except (duckdb.IOException, duckdb.ConnectionException) as exc:
            if not _is_busy(exc):
                raise CatalogError(f"Cannot open catalog {path}: {exc}") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CatalogError(
                    f"Catalog {path} is locked by another process (waited {busy_timeout:g}s)"
                ) from exc
            delay = _BUSY_RETRY_SECONDS[min(attempt, len(_BUSY_RETRY_SECONDS) - 1)]
            log.debug("catalog_busy", attempt=attempt, retry_in=delay)
            time.sleep(min(delay, remaining))
            attempt += 1


def _is_busy(exc: duckdb.Error) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _BUSY_MARKERS)


def open_catalog(config: AppConfig, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """A catalog connection ready for queries (the storage connector is configured on it)."""
    con = connect_catalog(
        config.paths.catalog_path, read_only, busy_timeout=config.catalog.busy_timeout_seconds
    )
    try:
        get_connector(config).configure(con)
    except Exception:
        con.close()
        raise
    return con


@contextmanager
def writable_catalog(config: AppConfig) -> Iterator[duckdb.DuckDBPyConnection]:
    """A short-lived read-write connection with the state tables created."""
    con = open_catalog(config, read_only=False)
    try:
        ensure_state(con)
        yield con
    finally:
        con.close()


# --------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------


def ensure_state(con: duckdb.DuckDBPyConnection) -> None:
    for ddl in STATE_DDL:
        con.execute(ddl)


def has_state(con: duckdb.DuckDBPyConnection) -> bool:
    found = con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name IN "
        f"({', '.join(repr(t) for t in STATE_TABLES)})"
    ).fetchone()
    return bool(found and found[0] == len(STATE_TABLES))


def active_version(con: duckdb.DuckDBPyConnection) -> DataVersion | None:
    if not has_state(con):
        return None
    row = con.execute(
        "SELECT version_id, label, fingerprint, created_at, data_through, validated, connector, "
        "refresh_id FROM _data_version ORDER BY version_id DESC LIMIT 1"
    ).fetchone()
    return DataVersion(*row) if row else None


def table_stats(con: duckdb.DuckDBPyConnection, version_id: int) -> list[TableStats]:
    rows = con.execute(
        "SELECT table_name, row_count, file_count, bytes, min_date, max_date FROM _table_stats "
        "WHERE version_id = ? ORDER BY table_name",
        [version_id],
    ).fetchall()
    return [TableStats(*row) for row in rows]


def active_files(con: duckdb.DuckDBPyConnection) -> dict[str, list[FileInfo]]:
    files: dict[str, list[FileInfo]] = {}
    for table, uri, size, modified in con.execute(
        "SELECT table_name, uri, size, modified FROM _files ORDER BY table_name, uri"
    ).fetchall():
        files.setdefault(table, []).append(FileInfo(uri, size, modified))
    return files


def refresh_history(con: duckdb.DuckDBPyConnection, limit: int = 5) -> list[RefreshLogEntry]:
    if not has_state(con):
        return []
    rows = con.execute(
        "SELECT refresh_id, started_at, finished_at, status, fingerprint, file_count, "
        "issue_count, version_id, message FROM _refresh_log "
        "ORDER BY started_at DESC, finished_at DESC LIMIT ?",
        [limit],
    ).fetchall()
    return [RefreshLogEntry(*row) for row in rows]


def record_refresh(con: duckdb.DuckDBPyConnection, entry: RefreshLogEntry) -> None:
    con.execute(
        "INSERT INTO _refresh_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            entry.refresh_id,
            entry.started_at,
            entry.finished_at,
            entry.status,
            entry.fingerprint,
            entry.file_count,
            entry.issue_count,
            entry.version_id,
            entry.message,
        ],
    )


def activate(
    con: duckdb.DuckDBPyConnection,
    *,
    files: Mapping[str, Sequence[FileInfo]],
    columns: Mapping[str, Sequence[str]],
    stats: Sequence[TableStats],
    fingerprint: str,
    created_at: datetime,
    data_through: date | None,
    validated: bool,
    connector: str,
    refresh_id: str,
) -> DataVersion:
    """Pin the views to ``files`` and record a new data version. Run inside a transaction."""
    create_file_views(con, {t: [f.uri for f in fs] for t, fs in files.items()}, columns)
    con.execute("DELETE FROM _files")
    rows = [(t, f.uri, f.size, f.modified) for t, fs in files.items() for f in fs]
    if rows:
        con.executemany("INSERT INTO _files VALUES (?, ?, ?, ?)", rows)

    previous = con.execute("SELECT coalesce(max(version_id), 0) FROM _data_version").fetchone()
    version_id = (previous[0] if previous else 0) + 1
    label = f"v{version_id} | {created_at:%Y-%m-%d %H:%M} UTC | {fingerprint[:8]}"
    version = DataVersion(
        version_id,
        label,
        fingerprint,
        created_at,
        data_through,
        validated,
        connector,
        refresh_id,
    )
    con.execute(
        "INSERT INTO _data_version VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            version.version_id,
            version.label,
            version.fingerprint,
            version.created_at,
            version.data_through,
            version.validated,
            version.connector,
            version.refresh_id,
        ],
    )
    if stats:
        con.executemany(
            "INSERT INTO _table_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (version_id, s.table, s.row_count, s.file_count, s.bytes, s.min_date, s.max_date)
                for s in stats
            ],
        )
    return version
