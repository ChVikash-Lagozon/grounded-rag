"""Refresh: make newly loaded files visible as a new data version.

Steps: scan the storage connector → fingerprint the file list (unchanged → ``no_change``) →
validate the scanned files on a scratch connection → reject on errors unless ``allow_invalid``
→ activate in one catalog transaction (pinned views, file list, stats, new version, log row) →
run the post-refresh hooks.

Loading itself is external (phase-0 §3), so there is no rollback: a rejected refresh just
leaves the previous version active. The CLI, and later the UI/API, all call :func:`refresh`.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

import duckdb

from carquery.catalog import (
    DataVersion,
    RefreshLogEntry,
    TableStats,
    activate,
    active_version,
    record_refresh,
    writable_catalog,
)
from carquery.config import AppConfig
from carquery.contract import SchemaContract
from carquery.datafiles import create_file_views
from carquery.datasets import Dataset
from carquery.logging import bound_context, get_logger
from carquery.storage import FileInfo, StorageConnector, get_connector
from carquery.validation import ValidationReport, validate_views

log = get_logger(__name__)

RefreshStatus = Literal["active", "rejected", "no_change", "error"]


@dataclass
class RefreshResult:
    refresh_id: str
    status: RefreshStatus
    version: DataVersion | None  # the version active after this refresh
    previous: DataVersion | None  # the version active before it
    fingerprint: str | None = None
    files: dict[str, list[FileInfo]] = field(default_factory=dict)
    stats: list[TableStats] = field(default_factory=list)
    report: ValidationReport | None = None
    message: str | None = None

    @property
    def file_count(self) -> int:
        return sum(len(files) for files in self.files.values())


PostRefreshHook = Callable[[RefreshResult, AppConfig], None]

# Run after every activation. Phase 4 registers its statistics builder, Phase 5 its cache
# invalidation. A failing hook is logged; it does not undo the activation.
POST_REFRESH_HOOKS: list[PostRefreshHook] = []


def scan(connector: StorageConnector, datasets: Sequence[Dataset]) -> dict[str, list[FileInfo]]:
    """The current files of every dataset, keyed by table name."""
    return {dataset.name: connector.list_files(dataset) for dataset in datasets}


def fingerprint(files: Mapping[str, Sequence[FileInfo]]) -> str:
    """SHA-256 over the sorted (table, uri, size, modified) list."""
    digest = hashlib.sha256()
    for table in sorted(files):
        for info in sorted(files[table], key=lambda f: f.uri):
            digest.update(
                f"{table}\t{info.uri}\t{info.size}\t{info.modified.isoformat()}\n".encode()
            )
    return digest.hexdigest()


def connect_scanned(
    connector: StorageConnector, files: Mapping[str, Sequence[FileInfo]]
) -> duckdb.DuckDBPyConnection:
    """An in-memory connection with a view per table over exactly the scanned files."""
    con = duckdb.connect()
    try:
        connector.configure(con)
        create_file_views(con, {table: [f.uri for f in fs] for table, fs in files.items()})
    except Exception:
        con.close()
        raise
    return con


def collect_stats(
    con: duckdb.DuckDBPyConnection,
    contract: SchemaContract,
    files: Mapping[str, Sequence[FileInfo]],
    report: ValidationReport,
) -> list[TableStats]:
    """Row count, file count, bytes and date range per table that has files."""
    stats = []
    for name, table in contract.tables.items():
        table_files = files.get(name, [])
        if not table_files:
            continue
        rows = report.row_counts.get(name)
        min_date = max_date = None
        try:
            if rows is None:
                rows = _scalar(con, f"SELECT count(*) FROM {name}")
            if table.date_column:
                min_date, max_date = con.execute(
                    f'SELECT min("{table.date_column}")::DATE, max("{table.date_column}")::DATE '
                    f"FROM {name}"
                ).fetchone() or (None, None)
        except duckdb.Error as exc:  # only reachable with invalid data (allow_invalid)
            log.warning("refresh_stats_failed", table=name, error=str(exc))
        stats.append(
            TableStats(
                name, rows, len(table_files), sum(f.size for f in table_files), min_date, max_date
            )
        )
    return stats


def data_through(contract: SchemaContract, stats: Sequence[TableStats]) -> date | None:
    """The latest date column value over all fact tables."""
    facts = {t.name for t in contract.facts}
    dates = [s.max_date for s in stats if s.table in facts and s.max_date]
    return max(dates) if dates else None


def refresh(
    config: AppConfig,
    contract: SchemaContract,
    datasets: Sequence[Dataset],
    *,
    force: bool = False,
    allow_invalid: bool = False,
    hooks: Sequence[PostRefreshHook] | None = None,
) -> RefreshResult:
    """Scan, validate and (if valid, changed or forced) activate a new data version."""
    refresh_id = uuid.uuid4().hex[:12]
    started = _now()
    connector = get_connector(config)
    result = RefreshResult(refresh_id, "error", None, None)
    with bound_context(refresh_id=refresh_id):
        log.info(
            "refresh_started",
            connector=connector.describe(),
            force=force,
            allow_invalid=allow_invalid,
        )
        try:
            with writable_catalog(config) as con:
                result.previous = result.version = active_version(con)

            result.files = scan(connector, datasets)
            result.fingerprint = fingerprint(result.files)
            log.info(
                "refresh_scanned", files=result.file_count, fingerprint=result.fingerprint[:12]
            )
            if _unchanged(result.previous, result.fingerprint, force):
                return _finish(config, result, started, "no_change", "no new or changed files")

            work = connect_scanned(connector, result.files)
            try:
                result.report = validate_views(work, contract)
                result.stats = collect_stats(work, contract, result.files, result.report)
            finally:
                work.close()
            if not result.report.ok and not allow_invalid:
                errors = sum(1 for i in result.report.issues if i.severity == "error")
                log.warning("refresh_rejected", errors=errors)
                return _finish(
                    config, result, started, "rejected", f"validation failed: {errors} error(s)"
                )

            if not _activate(config, contract, connector, result, started, force):
                return _finish(config, result, started, "no_change", "activated concurrently")
            assert result.version is not None
            log.info(
                "refresh_activated",
                version=result.version.version_id,
                data_through=str(result.version.data_through),
                validated=result.version.validated,
            )
        except Exception as exc:
            log.exception("refresh_failed")
            result.version = result.previous
            return _finish(config, result, started, "error", f"{type(exc).__name__}: {exc}")

        for hook in POST_REFRESH_HOOKS if hooks is None else hooks:
            try:
                hook(result, config)
            except Exception:
                log.exception("refresh_hook_failed", hook=getattr(hook, "__name__", repr(hook)))
        return result


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _activate(
    config: AppConfig,
    contract: SchemaContract,
    connector: StorageConnector,
    result: RefreshResult,
    started: datetime,
    force: bool,
) -> bool:
    """One catalog transaction; False if another refresh already activated these files."""
    assert result.fingerprint is not None and result.report is not None
    with writable_catalog(config) as con:
        con.begin()
        try:
            current = active_version(con)
            if _unchanged(current, result.fingerprint, force):
                con.rollback()
                result.version = current
                return False
            result.version = activate(
                con,
                files=result.files,
                columns={n: t.column_names for n, t in contract.tables.items()},
                stats=result.stats,
                fingerprint=result.fingerprint,
                created_at=_now(),
                data_through=data_through(contract, result.stats),
                validated=result.report.ok,
                connector=connector.describe(),
                refresh_id=result.refresh_id,
            )
            result.status = "active"
            record_refresh(con, _log_entry(result, started))
            con.commit()
        except Exception:
            con.rollback()
            raise
    return True


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0]) if row else 0


def _unchanged(version: DataVersion | None, fingerprint: str, force: bool) -> bool:
    return not force and version is not None and version.fingerprint == fingerprint


def _log_entry(result: RefreshResult, started: datetime) -> RefreshLogEntry:
    return RefreshLogEntry(
        refresh_id=result.refresh_id,
        started_at=started,
        finished_at=_now(),
        status=result.status,
        fingerprint=result.fingerprint,
        file_count=result.file_count if result.files else None,
        issue_count=len(result.report.issues) if result.report else None,
        version_id=result.version.version_id if result.version else None,
        message=result.message,
    )


def _finish(
    config: AppConfig,
    result: RefreshResult,
    started: datetime,
    status: RefreshStatus,
    message: str,
) -> RefreshResult:
    """Record a refresh that did not activate anything."""
    result.status, result.message = status, message
    try:
        with writable_catalog(config) as con:
            record_refresh(con, _log_entry(result, started))
    except Exception:
        log.exception("refresh_log_failed", status=status)
    log.info("refresh_finished", status=status, message=message)
    return result
