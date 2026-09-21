"""The query engine: run one untrusted SQL statement against the catalog, safely.

Layers, in order (docs/plans/phase-3.md):

1. **Statement checks** (no catalog needed): one statement, typed ``SELECT``, not ``PRAGMA`` /
   ``CALL``, within ``query.max_sql_chars``.
2. **Sandbox**, a fresh read-only catalog connection per call: ``allowed_paths`` is exactly
   the pinned files of the active version, external access is off, memory/threads are capped,
   and the configuration is locked. This is the security boundary: other files (including
   ``ground_truth.yaml``), ``COPY``, ``ATTACH``, ``INSTALL`` and ``SET`` all fail.
3. **EXPLAIN** of the wrapped query: catches syntax and name errors without reading data.
4. **Execution** of ``SELECT * FROM (<sql>) LIMIT max_rows + 1`` on a worker thread; on timeout
   the caller interrupts it. (Interrupting from a timer thread while the main thread fetches
   crashes CPython with DuckDB 1.5.5, so the query always runs on the worker.)

Failures raise :class:`QueryError`, whose ``kind``/``hint`` are meant to be fed back to an LLM.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

import duckdb

from carquery.catalog import CatalogError, DataVersion, active_version, open_catalog
from carquery.config import AppConfig
from carquery.logging import bound_context, get_logger

log = get_logger(__name__)
T = TypeVar("T")

ErrorKind = Literal[
    "empty",
    "too_long",
    "multiple_statements",
    "not_read_only",
    "syntax",
    "unknown_table",
    "unknown_column",
    "binder",
    "type_error",
    "forbidden",
    "timeout",
    "out_of_memory",
    "execution",
    "unavailable",
]

_HINTS: dict[str, str] = {
    "empty": "Send one SQL SELECT query.",
    "multiple_statements": "Send exactly one SELECT statement.",
    "not_read_only": "Only read-only SELECT / WITH queries are allowed.",
    "syntax": "Use DuckDB SQL syntax.",
    "type_error": "Check casts, date literals ('YYYY-MM-DD') and the column types.",
    "forbidden": "Only the catalog tables can be queried; files, settings and extensions are "
    "not accessible.",
    "timeout": "Simplify the query: filter early, aggregate, and avoid cross joins.",
    "out_of_memory": "Simplify the query: filter early, aggregate, and avoid cross joins.",
    "unavailable": "The data catalog is not ready; run `carq refresh`.",
}
_NO_RETRY = {"unavailable"}
_BLOCKED_FIRST_KEYWORDS = {"PRAGMA", "CALL"}  # typed SELECT by DuckDB, but run pragmas
_ERROR_PREFIX = re.compile(r"^[A-Za-z ]*Error:\s*")
_LINE_REF = re.compile(r"LINE (\d+):")
_MAX_MESSAGE_CHARS = 1500
_INTERRUPT_GRACE_SECONDS = 5.0


class QueryError(Exception):
    """A query that was rejected or failed, with enough structure to retry it."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        hint: str | None = None,
        sql: str | None = None,
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind: ErrorKind = kind
        self.message = message
        self.hint = hint if hint is not None else _HINTS.get(kind)
        self.sql = sql

    @property
    def retryable(self) -> bool:
        """Whether a corrected query could succeed (so it's worth sending back to an LLM)."""
        return self.kind not in _NO_RETRY

    def to_llm(self) -> str:
        text = f"Query error ({self.kind}): {self.message}"
        return f"{text}\nHint: {self.hint}" if self.hint else text

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
            "sql": self.sql,
        }


@dataclass(frozen=True)
class QueryResult:
    query_id: str
    sql: str
    columns: list[str]
    types: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    duration_ms: float
    data_version: DataVersion
    max_rows: int = field(repr=False)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


# --------------------------------------------------------------------------------------
# Statement checks
# --------------------------------------------------------------------------------------


def check_statement(sql: str, max_chars: int) -> str:
    """Reject anything but one read-only query; return it without trailing ``;``."""
    text = sql.strip()
    if not text:
        raise QueryError("empty", "the query is empty")
    if len(text) > max_chars:
        raise QueryError(
            "too_long",
            f"the query has {len(text):,} characters (limit {max_chars:,})",
            hint=f"Send one SQL query under {max_chars:,} characters.",
        )
    try:
        statements = duckdb.extract_statements(text)
        tokens = duckdb.tokenize(text)  # comments are not tokens
    except duckdb.Error as exc:
        raise _classify(exc) from exc
    if not statements:
        raise QueryError("empty", "the query contains no statement")
    if len(statements) > 1:
        raise QueryError("multiple_statements", f"found {len(statements)} statements")
    kind = statements[0].type.name
    first = re.match(r"\w+", text[tokens[0][0] :]) if tokens else None
    first_word = first.group(0).upper() if first else ""
    if kind != "SELECT" or first_word in _BLOCKED_FIRST_KEYWORDS:
        raise QueryError("not_read_only", f"{first_word or kind} statements are not allowed")

    # Cut trailing ';' (and any comment after it) so the query can be wrapped as a subquery.
    end = len(tokens)
    while end > 0 and text[tokens[end - 1][0]] == ";":
        end -= 1
    cut = tokens[end][0] if end < len(tokens) else len(text)
    return text[:cut].rstrip()


def wrap(sql: str, limit: int) -> str:
    """Limit a checked query; newlines keep a trailing ``--`` comment inside the subquery."""
    return f"SELECT * FROM (\n{sql}\n) AS q LIMIT {limit}"


_WRAP_LINES = 1  # lines ``wrap`` puts before the query; error positions are shifted back


# --------------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------------


class QueryEngine:
    """Runs queries on the catalog with the limits in ``config.query``. Thread-safe: every
    call uses its own connection, and nothing is held between calls (refresh never waits)."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.limits = config.query

    def validate(self, sql: str, *, timeout: float | None = None) -> QueryError | None:
        """Statement checks plus EXPLAIN. Returns the error instead of raising it."""
        query_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        with bound_context(query_id=query_id):
            try:
                inner = check_statement(sql, self.limits.max_sql_chars)
                deadline = started + self._timeout(timeout)
                with self._session() as session:
                    session.run(
                        lambda c: c.execute("EXPLAIN " + wrap(inner, 1)).fetchall(), deadline
                    )
            except QueryError as err:
                err.sql = sql
                self._log_failure(err, "validate", started)
                return err
            log.info("query_validated", duration_ms=_ms(started))
            return None

    def execute(
        self, sql: str, *, max_rows: int | None = None, timeout: float | None = None
    ) -> QueryResult:
        """Run ``sql`` and return at most ``max_rows`` rows; raises :class:`QueryError`."""
        query_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        rows_limit = min(max_rows or self.limits.max_rows, self.limits.max_rows)
        if rows_limit < 1:
            raise ValueError("max_rows must be at least 1")
        stage = "check"
        with bound_context(query_id=query_id):
            try:
                inner = check_statement(sql, self.limits.max_sql_chars)
                wrapped = wrap(inner, rows_limit + 1)
                deadline = started + self._timeout(timeout)
                with self._session() as session:
                    if self.limits.explain_before_execute:
                        stage = "explain"
                        session.run(lambda c: c.execute("EXPLAIN " + wrapped).fetchall(), deadline)
                    stage = "execute"
                    description, rows = session.run(lambda c: _fetch(c, wrapped), deadline)
                    version = session.version
            except QueryError as err:
                err.sql = sql
                self._log_failure(err, stage, started)
                raise

            result = QueryResult(
                query_id=query_id,
                sql=sql,
                columns=[d[0] for d in description],
                types=[str(d[1]) for d in description],
                rows=rows[:rows_limit],
                truncated=len(rows) > rows_limit,
                duration_ms=_ms(started),
                data_version=version,
                max_rows=rows_limit,
            )
            log.info(
                "query_executed",
                duration_ms=result.duration_ms,
                rows=result.row_count,
                truncated=result.truncated,
                data_version=version.version_id,
                sql=self._loggable(sql),
            )
            return result

    # ----------------------------------------------------------------------------------

    def _timeout(self, requested: float | None) -> float:
        if requested is None:
            return self.limits.timeout_seconds
        if requested <= 0:
            raise ValueError("timeout must be positive")
        return min(requested, self.limits.timeout_seconds)

    @contextmanager
    def _session(self) -> Iterator[_Session]:
        sandbox = _SANDBOXES.acquire(self.config)
        session = _Session(sandbox.con.cursor(), sandbox.version, sandbox.tables)
        try:
            yield session
        finally:
            if not session.stuck:  # closing under a running query is unsafe; leak it instead
                session.con.close()
            _SANDBOXES.release(sandbox, leaked=session.stuck)

    def _loggable(self, sql: str) -> str:
        limit = self.limits.log_sql_max_chars
        return sql if len(sql) <= limit else sql[:limit] + "..."

    def _log_failure(self, err: QueryError, stage: str, started: float) -> None:
        log.info(
            "query_failed",
            stage=stage,
            kind=err.kind,
            message=err.message[:300],
            duration_ms=_ms(started),
            sql=self._loggable(err.sql or ""),
        )


@dataclass
class _Sandbox:
    con: duckdb.DuckDBPyConnection
    version: DataVersion
    tables: list[str]
    users: int = 0
    leaked: bool = False  # a query could not be interrupted; never close this instance


class _Sandboxes:
    """Sandboxed read-only catalog instances, one per catalog file, shared by the queries
    running at the same time in this process.

    Within a process DuckDB keeps one database instance per file, and the sandbox settings
    (``allowed_paths``, ``lock_configuration`` …) are instance-wide. So concurrent queries share
    one sandboxed instance, each on its own cursor, and it is closed when the last one finishes:
    an idle engine never blocks refresh. The first engine to open it sets memory/threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[Path, _Sandbox] = {}

    def acquire(self, config: AppConfig) -> _Sandbox:
        path = config.paths.catalog_path
        with self._lock:
            sandbox = self._open.get(path)
            if sandbox is None:
                sandbox = self._open[path] = _open_sandbox(config)
            sandbox.users += 1
            return sandbox

    def release(self, sandbox: _Sandbox, leaked: bool) -> None:
        with self._lock:
            sandbox.users -= 1
            sandbox.leaked = sandbox.leaked or leaked
            if sandbox.users == 0 and not sandbox.leaked:
                self._open = {p: s for p, s in self._open.items() if s is not sandbox}
                sandbox.con.close()


_SANDBOXES = _Sandboxes()


def _open_sandbox(config: AppConfig) -> _Sandbox:
    """Open the catalog read-only, read the active version, then lock it down (§4.1)."""
    try:
        con = open_catalog(config)
    except CatalogError as exc:
        raise QueryError("unavailable", str(exc)) from exc
    try:
        version = active_version(con)
        if version is None:
            raise QueryError("unavailable", "the catalog has no active data version")
        files = [row[0] for row in con.execute("SELECT uri FROM _files").fetchall()]
        tables = [
            row[0]
            for row in con.execute(
                "SELECT view_name FROM duckdb_views() WHERE NOT internal ORDER BY view_name"
            ).fetchall()
        ]
        limits = config.query
        con.execute(f"SET allowed_paths = [{', '.join(_literal(uri) for uri in files)}]")
        con.execute("SET enable_external_access = false")
        con.execute(f"SET memory_limit = {_literal(limits.memory_limit)}")
        if limits.threads is not None:
            con.execute(f"SET threads = {int(limits.threads)}")
        con.execute("SET lock_configuration = true")
    except BaseException:
        con.close()
        raise
    return _Sandbox(con, version, tables)


class _Session:
    """One query's cursor on the shared sandbox, plus what the error hints need."""

    def __init__(
        self, cursor: duckdb.DuckDBPyConnection, version: DataVersion, tables: list[str]
    ) -> None:
        self.con = cursor
        self.version = version
        self.tables = tables
        self.stuck = False

    def run(self, work: Callable[[duckdb.DuckDBPyConnection], T], deadline: float) -> T:
        """Run ``work`` on a worker thread; interrupt it from here when the deadline passes."""
        outcome: dict[str, Any] = {}

        def target() -> None:
            try:
                outcome["value"] = work(self.con)
            except BaseException as exc:  # handed back to the calling thread
                outcome["error"] = exc

        worker = threading.Thread(target=target, name="carq-query", daemon=True)
        worker.start()
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            self.con.interrupt()
            worker.join(_INTERRUPT_GRACE_SECONDS)
            self.stuck = worker.is_alive()
            if self.stuck:
                log.warning("query_interrupt_stuck")
            raise QueryError("timeout", "the query exceeded its time limit and was cancelled")
        error = outcome.get("error")
        if isinstance(error, duckdb.InterruptException):
            raise QueryError("timeout", "the query was cancelled") from error
        if isinstance(error, duckdb.Error):
            raise _classify(error, self.tables, line_offset=_WRAP_LINES) from error
        if error is not None:
            raise error
        return outcome["value"]


def _fetch(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[list[Any], list[tuple[Any, ...]]]:
    cursor = con.execute(sql)
    rows = cursor.fetchall()
    return list(cursor.description or []), rows


def _classify(
    exc: duckdb.Error, tables: list[str] | None = None, line_offset: int = 0
) -> QueryError:
    message = _ERROR_PREFIX.sub("", str(exc).strip(), count=1)[:_MAX_MESSAGE_CHARS]
    if line_offset:  # report positions in the caller's SQL, not in the wrapped query
        message = _LINE_REF.sub(lambda m: f"LINE {int(m[1]) - line_offset}:", message)
    if isinstance(exc, duckdb.ParserException | duckdb.SyntaxException):
        return QueryError("syntax", message)
    if isinstance(exc, duckdb.CatalogException):
        if message.startswith("Table with name"):
            hint = "Available tables: " + ", ".join(tables) if tables else None
            return QueryError("unknown_table", message, hint=hint)
        return QueryError("binder", message)
    if isinstance(exc, duckdb.BinderException):
        if "not found" in message or "does not exist" in message:
            return QueryError(
                "unknown_column", message, hint="Use only columns of the tables in FROM/JOIN."
            )
        return QueryError("binder", message)
    if isinstance(
        exc,
        duckdb.ConversionException | duckdb.TypeMismatchException | duckdb.InvalidTypeException,
    ):
        return QueryError("type_error", message)
    if isinstance(exc, duckdb.PermissionException):
        return QueryError("forbidden", message)
    if isinstance(exc, duckdb.OutOfMemoryException):
        return QueryError("out_of_memory", message)
    if isinstance(exc, duckdb.InterruptException):
        return QueryError("timeout", message)
    if isinstance(exc, duckdb.InvalidInputException) and "configuration" in message:
        return QueryError("forbidden", message)
    return QueryError("execution", message)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)
