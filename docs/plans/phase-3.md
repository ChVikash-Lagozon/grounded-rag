# Phase 3 Plan — Query Execution Layer

Status: **approved 2026-09-21, all defaults in §9 accepted — implemented** (changes: §0; decisions D-26…D-35). Branch: `phase-3-query-execution`.

## 0. Changes made during implementation

- **One shared sandbox per process, not one connection per query (§4.1).** Within a process,
  DuckDB keeps one database instance per file, and the sandbox settings are instance-wide.
  Once a query had set `lock_configuration`, a concurrent query could no longer apply its
  own settings. Concurrent queries now share one sandboxed instance, each on its own cursor
  (so they can be interrupted separately), and it is closed when the last one finishes.
  An idle engine therefore still never blocks refresh.
- **In-process refresh vs a running query:** DuckDB reports this as "different configuration
  than existing connections". The catalog treats that as *busy*, so the refresh waits
  (`catalog.busy_timeout_seconds`) instead of failing.
- **New dependency `pytz`** (small, pure Python). DuckDB needs it to return
  `TIMESTAMP WITH TIME ZONE` values, so without it any query using `now()` failed.
- **The catalog file itself is readable.** DuckDB always lets a connection read its own
  database file and `.wal` (`read_blob('catalog.duckdb')`). They contain nothing the views
  don't already expose.
- **Error positions are shifted back:** `LINE n` in DuckDB messages refers to the wrapped
  query, so it is reduced by the wrapper's one line.
- **Logging fix (Phase 0 bug):** `get_logger()` bound eagerly, so module-level loggers ignored
  `configure_logging` and printed to stdout. That corrupted `--format json` and bypassed
  `logging.file`. It now returns structlog's lazy proxy.

## 1. Goal and scope

A `QueryEngine` that runs one untrusted SQL statement (from an LLM in Phase 5) against the
catalog **safely, within limits, and with errors an LLM can act on**.

From the brief (§6 Phase 3): read-only enforcement (only SELECT / WITH), a configurable row
limit and timeout, EXPLAIN-based validation before execution, structured error objects that
can be fed back to an LLM, and query logging (SQL, duration, rows).

In scope:

- `carquery.query`: `QueryEngine`, `QueryResult`, `QueryError`;
- a `query:` config section;
- `carq query "SQL"`, which replaces the dev-only `carq catalog sql` from Phase 2.

Out of scope:

- LLM calls, retries and the result cache (Phase 5);
- JSON serialisation for the API and the UI table (Phase 7);
- query history persisted anywhere other than the logs (see open question 4).

## 2. Dependencies

None. Everything uses DuckDB features, which I checked on 1.5.5 (the installed version).

## 3. What the probes showed (DuckDB 1.5.5, Windows)

These results drive the design, so they are recorded here:

| Check | Result |
|---|---|
| `read_only=True` connection | Blocks `INSERT/UPDATE/DELETE/CREATE` on the catalog. **Does not** block reading other files or `COPY … TO` a file. |
| `SET allowed_paths = [<pinned files>]` + `SET enable_external_access = false` + `SET lock_configuration = true` | Views over the pinned files work, including joins. **Blocked:** `read_text('data/ground_truth.yaml')`, `read_csv`/`glob` anywhere else, `COPY … TO` (even inside `data/`), `ATTACH`, `INSTALL`, stored secrets, and any `SET` afterwards. |
| `allowed_directories = [data_root]` instead | Too loose: it **allows** reading `ground_truth.yaml` (the planted answers) and `COPY` into `data/`. Not used. |
| `duckdb.extract_statements()` | Types statements without running them. `WITH`, `FROM x`, `UNION`, `DESCRIBE`, `SUMMARIZE`, `SHOW` and `PRAGMA` all report as `SELECT`. `PIVOT` becomes `CREATE`+`SELECT`, and `INSERT` is `INSERT`. |
| `EXPLAIN <sql>` | Raises `ParserException`, `CatalogException` (unknown table, with a "Did you mean" suggestion) or `BinderException` (unknown column, with candidates) **without reading data**. |
| `con.interrupt()` | ⚠️ **Crashes Python** ("Fatal Python error: PyEval_SaveThread") if a timer thread calls it while the **main** thread is inside `fetchmany()` on a streaming result. Running the query on a **worker thread** and calling `interrupt()` from the caller after `join(timeout)` works cleanly for `fetchall` and `fetch_arrow_table`. It stops within about 10 ms, and the connection stays usable. |
| Wrapping in `SELECT * FROM (<sql>) AS q LIMIT n` | Works for `WITH`, `FROM x`, `DESCRIBE`, `SUMMARIZE` and `SHOW`, and keeps the inner `ORDER BY`. It fails on a trailing `;` or `--` comment unless the SQL is placed on its own lines with trailing `;` removed. `PRAGMA` cannot be wrapped. Duplicate output names are renamed (`a`, `a_1`). |
| Open + close of a read-only catalog connection | About 15 ms. |

## 4. Design — `carquery.query`

### 4.1 Per-query connection and sandbox

Every `execute()` and `validate()` call opens its **own** read-only catalog connection and
closes it afterwards:

1. `open_catalog(config)`, which also runs `connector.configure` (future ADLS secrets must be
   set before the lock);
2. read the active `DataVersion` and the pinned file list (`_files`) on that connection, so the
   version reported and the data queried always match;
3. `SET allowed_paths = [<pinned files>]`, `enable_external_access = false`,
   `memory_limit`, `threads`, then `lock_configuration = true`.

This also fixes the Phase 2 limitation that a long-lived reader blocks refresh: the engine
never holds the file between queries. Views pinned by refresh keep working, because
`allowed_paths` is exactly their file list.

### 4.2 Statement checks (before touching the catalog)

In order, each failure producing a `QueryError`:

1. not empty, and at most `max_sql_chars` characters;
2. `extract_statements` gives exactly one statement (`multiple_statements`) of type `SELECT`
   (`not_read_only`);
3. the first keyword is not `PRAGMA` or `CALL` (`not_read_only`). These are typed `SELECT` but
   run arbitrary pragmas.

The sandbox is the real security boundary. These checks exist to give the LLM a clear error
instead of a permission error, and to keep `PIVOT`/`CREATE`/`COPY` out.

### 4.3 Validation — `engine.validate(sql) -> QueryError | None`

Statement checks, then `EXPLAIN` of the **wrapped** SQL (4.4) on a sandboxed connection. This
catches syntax errors, unknown tables and columns, and type errors that show up at bind time,
without scanning data. `execute()` runs it first when `query.explain_before_execute` is true
(the default). Phase 5's step 3 ("validate → retry with error") calls it directly.

### 4.4 Execution — `engine.execute(sql, *, max_rows=None, timeout=None) -> QueryResult`

- Wrap: `SELECT * FROM (\n<sql without trailing ;>\n) AS q LIMIT <max_rows + 1>`. The newlines
  make a trailing `--` comment harmless. The extra row sets `truncated = True`. The wrapper's
  LIMIT goes into the plan, so memory stays bounded and `ORDER BY … LIMIT` becomes a top-N.
- Timeout: run `execute(...).fetchall()` on a worker thread, `join(timeout)`, then call
  `con.interrupt()` from the calling thread and raise `QueryError(kind="timeout")`. The timeout
  covers EXPLAIN plus execution. Never interrupt from a timer thread (§3).
- The result keeps column names and DuckDB type names from `description`.

`QueryResult` (a frozen dataclass):

| Field | Meaning |
|---|---|
| `query_id` | 12-character hex, also bound to logs |
| `sql` | the SQL as submitted |
| `columns`, `types` | output names, DuckDB type strings |
| `rows` | `list[tuple]` of Python values (`date`, `Decimal`, …) |
| `row_count`, `truncated` | rows returned (≤ max_rows); whether more existed |
| `duration_ms` | total wall time (validation + execution) |
| `data_version` | the `DataVersion` the query ran against |

Plus `to_records()` (list of dicts). No pandas: Streamlit and FastAPI can build tables from
`columns`/`rows` in Phase 7.

### 4.5 Structured errors — `QueryError(Exception)`

Fields: `kind`, `message` (DuckDB's message with the `X Error:` prefix removed, keeping its
`LINE 1: … ^` pointer and "Did you mean" candidates), `hint` (our advice), `retryable`,
`sql`. Methods: `to_llm()` returns compact text for the retry prompt, and `to_dict()` is for
logs and the API.

| `kind` | Raised when | `hint` (example) | retryable |
|---|---|---|---|
| `empty`, `too_long` | size checks | "Send one SQL query under N characters" | yes |
| `multiple_statements` | more than one statement | "Send exactly one SELECT statement" | yes |
| `not_read_only` | not SELECT, or PRAGMA/CALL | "Only SELECT/WITH queries are allowed" | yes |
| `syntax` | `ParserException` | "Use DuckDB SQL syntax" | yes |
| `unknown_table` | `CatalogException` | lists the available tables | yes |
| `unknown_column` | `BinderException` "not found" | DuckDB's candidate columns | yes |
| `binder` | other `BinderException` (ambiguous name, GROUP BY misuse …) | — | yes |
| `type_error` | `ConversionException`, `MismatchException` … | "Check casts and date literals" | yes |
| `forbidden` | `PermissionException` (sandbox) | "Only the catalog tables can be queried; files and settings are not accessible" | yes |
| `timeout` | exceeds `timeout_seconds` | "Simplify: filter earlier, aggregate, avoid cross joins" | yes |
| `out_of_memory` | `OutOfMemoryException` | as for timeout | yes |
| `execution` | any other DuckDB runtime error | — | yes |
| `unavailable` | no catalog / no active version / catalog busy | "Run `carq refresh`" | **no** |

### 4.6 Logging

Events use `bound_context(query_id=…)`:

- `query_validated` or `query_rejected` (kind, message);
- `query_executed` (duration_ms, rows, truncated, data_version, sql);
- `query_failed` (kind, message, duration_ms, sql).

SQL is logged in full, up to `log_sql_max_chars`. The existing `logging.file` setting already
persists these events as JSON lines. Phase 5 adds per-request logging (tokens, cost) on top.

## 5. Configuration

A new `query:` section in `app.yaml` (a `QueryConfig` model; each value can be overridden with
`CARQ__QUERY__…`):

```yaml
query:
  max_rows: 1000               # rows returned; one extra row is fetched to detect truncation
  timeout_seconds: 30          # validation + execution, per query
  memory_limit: 2GB            # DuckDB memory_limit for query connections
  threads: null                # null = DuckDB default (all cores)
  max_sql_chars: 20000
  explain_before_execute: true
  log_sql_max_chars: 4000
```

`execute()` can lower `max_rows`/`timeout` per call but never raise them above the config
values, so a caller (or a prompt-injected LLM) can't widen the limits.

## 6. CLI

```bash
carq query "SELECT ..." [--max-rows N] [--timeout S] [--explain] [--format table|json]
```

- `table` output is the ASCII table from Phase 2, plus a footer with rows, truncation, ms and
  data version.
- `json` output is `{columns, types, rows, truncated, duration_ms, data_version}` (dates as ISO
  strings). Useful for scripts and eval debugging.
- `--explain` runs `validate()` only and prints "valid" or the error.
- On an error it prints kind, message and hint, and exits 1.
- `carq catalog sql` is **removed**, because it was the unguarded placeholder (open question 3).

## 7. Files

```
config/app.yaml               # + query: section
src/carquery/config.py        # + QueryConfig
src/carquery/query.py         # QueryEngine, QueryResult, QueryError, statement checks, sandbox
src/carquery/cli.py           # + carq query, − carq catalog sql
tests/test_query.py           # engine, errors, limits, timeout
tests/test_query_security.py  # sandbox: one test per blocked capability
tests/test_catalog.py         # CLI test moves from catalog sql to query
```

## 8. Tests

All tests use `catalog_setup` plus one refresh (the `test` preset), so no LLM and no network
are involved.

- **Results:** correct rows, columns and types for a join and aggregate. `ORDER BY` is kept
  through the wrapper. `max_rows` truncation sets `truncated`. A per-call `max_rows` above the
  config is clamped. The data version equals the catalog's active version. `WITH`, `FROM x`,
  `DESCRIBE` and `SUMMARIZE` work. A trailing `;` or `-- comment` is fine.
- **Errors (one per kind):** the right `kind`, `retryable` and a helpful hint (for example,
  `unknown_table` lists real tables and `unknown_column` includes DuckDB's candidates).
  `to_llm()` is compact and contains the message.
- **Security (each must fail with `not_read_only` or `forbidden`, and leave files unchanged):**
  `INSERT/UPDATE/DELETE/DROP/CREATE`, `COPY … TO`, `ATTACH`, `INSTALL`/`LOAD`, `SET`, `PRAGMA`,
  `CALL`, multiple statements, `read_text`/`read_csv`/`glob` on `ground_truth.yaml` and on a
  path outside `data/`, a replacement scan `FROM 'file.csv'`, and `EXPORT DATABASE`.
- **Timeout:** `SELECT … FROM range(10^11) WHERE …` with a 0.3 s timeout raises `timeout` in
  under about 2 s, and the next query on the engine succeeds. This also guards against the
  interrupt crash.
- **Validation:** `validate()` reports the same errors as `execute()` and doesn't read data.
- **Concurrency:** refresh succeeds while the engine is idle between queries (no held
  connection).
- **CLI:** table and json output, `--explain`, and exit code 1 with the kind shown on errors.

## 9. Open questions (defaults in bold)

1. **Row limit 1000 by default, and truncation is a flag, not an error.** The LLM path in
   Phase 5 mostly produces aggregates. The UI shows "first 1000 rows".
2. **Wrap the query in `SELECT * FROM (…) LIMIT n+1`** (bounded memory, top-N plans), rather
   than streaming with `fetchmany` (keeps duplicate column names, but can't bound the work
   and needs care with interrupts). Side effect: duplicate output column names get suffixes
   (`a_1`), which also helps dataframes.
3. **Replace `carq catalog sql` with `carq query`**, rather than keeping both.
4. **Query log is structured log events only** (persisted via `logging.file` as JSON lines).
   The alternative is a query-history table in a separate DuckDB file. The catalog itself
   stays read-only for queries. Phase 5 decides whether the UI needs a history.
5. **Queries may read the `_`-prefixed state tables** (`_data_version` etc.). They are
   harmless, and useful for "how fresh is the data?". The alternative is to hide them.
6. **Allow `DESCRIBE`, `SUMMARIZE` and `SHOW`.** They are read-only and let the LLM explore;
   the timeout bounds `SUMMARIZE` on big tables.
7. **Defaults: `timeout_seconds: 30`, `memory_limit: 2GB`, `threads: null`** (fits a laptop at
   `small`/`medium` scale; Phase 8 revisits them).
8. **ADLS later:** `allowed_paths` with `abfss://` URIs has not been tested. When the ADLS
   connector arrives, the sandbox may need `allowed_directories` scoped to the container
   prefix. It's noted here so it isn't forgotten, and nothing is built now.

## 10. Definition of done

`carq query` runs guarded queries with limits, a timeout and structured errors. Every blocked
capability in §8 has a passing test. `uv run pytest` is green and ruff is clean. README,
CLAUDE.md and PROGRESS.md are updated. Then one commit on `phase-3-query-execution`; a push
or PR only when you ask.
