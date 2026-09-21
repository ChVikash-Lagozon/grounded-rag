# Findings and lessons learned

What the project taught us: verified technical behaviour, bugs found and how they were
caught, and the risks still open. It is written to be reused, for example in the final
presentation. **Keep it current.** Add an entry whenever a probe, test or bug teaches
something that isn't obvious from the code.

Each entry names the phase, the evidence, and what we changed.

## DuckDB behaviour (verified on 1.5.5, Windows 11)

| # | Finding | Evidence | Consequence |
|---|---|---|---|
| F-01 | A `read_only` connection doesn't stop reading **other** files or `COPY … TO` a file. | Phase 3 probe: `read_text('data/ground_truth.yaml')` and `COPY` both succeeded. | The sandbox uses `allowed_paths` (D-26). |
| F-02 | `allowed_directories = [data_root]` is too loose. It allows reading `ground_truth.yaml` (the planted answers) and writing into `data/`. `allowed_paths` limited to the exact pinned files blocks both. | Phase 3 probe. | Pinned file lists (D-20) made a tight sandbox possible. |
| F-03 | A connection can always read its **own** database file and `.wal`, even when they're outside `allowed_paths`. | Security test (`read_blob('catalog.duckdb')`). | Accepted: they hold nothing the views don't expose. |
| F-04 | `con.interrupt()` from a timer thread while the main thread is in `fetchmany()` **crashes CPython** ("Fatal Python error: PyEval_SaveThread"). Interrupting a worker thread from the caller is safe, stops in about 10 ms, and leaves the connection usable. | Phase 3 probe, one process per strategy. | Query timeout design (D-28), and a CLAUDE.md warning. |
| F-05 | Within one process, all connections to the same file share **one database instance**. Settings such as `allowed_paths`, `lock_configuration`, `memory_limit` are instance-wide. | `test_parallel_queries` failed: "configuration has been locked". | Shared sandbox per process (D-31). |
| F-06 | Opening a file read-write while it's open read-only in the same process (or the other way round) fails with "different configuration than existing connections". Across processes it's the file lock. On Windows the lock error reads "being used by another process", not "lock". | Phase 2 lock test and Phase 3 probe. | Both messages count as *busy* and are retried (D-22). |
| F-07 | `extract_statements` types `DESCRIBE`, `SUMMARIZE`, `SHOW`, `FROM x` **and `PRAGMA`** as `SELECT`. `PIVOT` is `CREATE` + `SELECT`. `.query` rewrites `PRAGMA` to `SELECT * FROM pragma_…()`. | Phase 3 probe. | The PRAGMA/CALL check uses the original text. |
| F-08 | `duckdb.tokenize` skips comments and handles `--` inside strings correctly. | Phase 3 probe. | Used to strip trailing `;` and comments safely. |
| F-09 | Wrapping a query in a subquery renames duplicate output columns (`a`, `a_1`) and shifts error line numbers by one. | Tests. | Line numbers are mapped back. The renaming is documented. |
| F-10 | `EXPLAIN` doesn't catch errors from constant folding (`SELECT 'x'::INT` passes EXPLAIN, then fails at run time). | Smoke test. | Such failures still surface as `type_error` at execution. |
| F-11 | Returning `TIMESTAMP WITH TIME ZONE` to Python needs `pytz`. | Security test (`read_blob` returns a TIMESTAMPTZ). | `pytz` dependency (D-35). |
| F-12 | A read-only catalog connection opens and closes in about 15 ms. | Phase 3 probe. | Short-lived connections are cheap, so the engine never holds the catalog while idle. |

## Bugs caught (and how)

| # | Bug | Caught by | Fix |
|---|---|---|---|
| B-01 | The catalog busy-retry never ran on Windows: it matched only the word "lock". | Phase 2 cross-process lock test. | Match both OS messages (F-06). |
| B-02 | `carq catalog sql` crashed on Windows: DuckDB's box-drawing table output isn't cp1252. | Phase 2 smoke test. | ASCII table renderer (D-25). |
| B-03 | Concurrent queries failed after the first locked the shared instance's configuration. | Phase 3 `test_parallel_queries`. | D-31. |
| B-04 | **Phase 0 bug:** `get_logger()` bound eagerly, so every module-level logger kept structlog's defaults. Logs went to **stdout** (corrupting `carq query --format json`) and never reached `logging.file`. | Phase 3 CLI JSON test, then confirmed in a real shell. | `get_logger` returns the lazy proxy, with a regression test. |
| B-05 | Any query returning `now()` failed. | Phase 3 security test (indirectly). | F-11 / D-35. |

## Process lessons

- **Probe before planning.** Every Phase 3 design decision (sandbox, timeout, wrapping) came
  from a few minutes of throwaway scripts. Two of the planned approaches (a `read_only`
  sandbox, a timer-based interrupt) would have been insecure or crashed.
- **Test what the plan assumes.** The planned "one connection per query" design was wrong
  (F-05). A parallel-query test found this in seconds.
- **Run each risky probe in its own process with a hard timeout.** One probe hung and then
  crashed the interpreter. A separate process kept the session intact.
- **Test on the platform you use.** Three of the bugs above are Windows-specific (B-01, B-02,
  and the console encoding behind D-23).

## Open risks

| Risk | Where | Plan |
|---|---|---|
| The sandbox is untested with `abfss://` (ADLS) paths. | D-19, D-26 | Verify when the ADLS connector lands. It may need `allowed_directories` scoped to the container prefix. |
| Dimensions are overwritten in place, so the views see new dimension contents before a refresh. | D-02, D-20 | Inherent to D-02. Documented. |
| A query that ignores an interrupt for 5 s leaks its connection (the catalog stays open until the process exits). | D-28 | Logged as `query_interrupt_stuck`. Never observed. |
| The first engine in a process sets memory/threads for the shared sandbox. | D-31 | Keep one `query:` config per process (Phase 7 apps). |
| Free-tier LLM quota (about 50 requests/day) limits evaluation throughput. | D-07 | Phase 6: daily budget, resumable runs, smoke subset. |
