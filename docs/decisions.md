# Decision log

Every decision that shaped the project, in one place. The phase plans (`plans/phase-N.md`)
hold the full reasoning. This log is the index: what was decided, why, and what it
replaced. **Keep it current.** Add a row whenever a plan is approved or implementation
departs from it.

Status: ✅ in effect · 🔁 replaced (see the row that replaces it) · ⏸ deferred

## Project-level (Phase 0, 2026-09-21)

| ID | Decision | Why | Instead of | Status |
|---|---|---|---|---|
| D-01 | `config/datasets.yaml` holds static definitions only. Runtime state (data version, refresh log) lives in the `.duckdb` catalog. | Git-tracked config must not change daily. State belongs with the data. | The brief's manifest carrying the "active load version" | ✅ |
| D-02 | **Loading is external.** Facts are append-only files, dimensions are full reloads at fixed paths. We build only **refresh**. | The upstream platform owns loading and history. The PoC shouldn't re-implement it. | The brief's loader with version folders, atomic switch, rollback, retention | ✅ |
| D-03 | Hive `year=/month=` partitions for large facts (per-table setting). No `load_date` versioning layer. | One partitioning scheme. Versioning is refresh's job (D-01, D-02). | Two schemes on the same facts | ✅ |
| D-04 | `dim_date` key is `calendar_date`, a documented exception to "identical join key names". | `date` is a SQL keyword. `dim_date` is a role-playing dimension. | `dim_date.date` | ✅ |
| D-05 | Recall split into `dim_recall_campaign` + `fact_recall_vehicle`. | The brief's grain repeated campaign attributes on every VIN row. | One `fact_recall` table | ✅ |
| D-06 | The fiscal year starts 1 April. Apr 2025–Mar 2026 = **FY2026**. | Named by the year it ends in. Golden-question answers depend on it. | FY2025 | ✅ |
| D-07 | LLM: dev on the OpenRouter free tier (about 50 requests/day), prod on Azure AI Foundry. | Cost. Prod is a config change. | Anthropic-specific | ✅ |
| D-08 | One generic `LLMProvider`: `OpenAICompatibleProvider` + `MockProvider`. Native adapters can come later. | Models will be swapped for testing, and OpenAI-compatible covers both targets. | Building `AnthropicProvider` now | ✅ |
| D-09 | Few-shot retrieval: BM25/keyword behind a `Retriever` interface. | No embedding model in the core install. Embeddings can plug in later. | Embeddings now | ✅ |
| D-10 | Remote `origin` = github.com/ChVikash-Lagozon/grounded-rag. Push only when asked. | — | — | ✅ |
| D-11 | Keep the repo outside OneDrive (`C:\dev\grounded-rag`). | DuckDB files and `.venv` break under sync. | OneDrive folder | ✅ |

## Phase 1 — schema contract, generator, validation (2026-09-21)

| ID | Decision | Why | Instead of | Status |
|---|---|---|---|---|
| D-12 | `config/schema_contract.yaml` is the **single source of truth** for tables. The ER diagram and schema doc are generated from it, and a test fails if they're stale. | No drift between data, docs and (later) LLM context. | Hand-written schema docs | ✅ |
| D-13 | Deterministic generator: one simulated timeline, sliced by date, and `rng_for(seed, stream, chunk)`. | Same seed means byte-identical files, and `generate day N` matches the history. | Global random state | ✅ |
| D-14 | 8 planted patterns (P1–P8), each with detection SQL in `ground_truth.yaml` and a test. | Makes "can the LLM find it?" measurable in Phase 6. | Random data only | ✅ |
| D-15 | Merging and restating rows is upstream's job. A row's final state is set when it is written. | Follows from D-02. No merge logic here. | Restated rows with "latest per key wins" | ✅ |
| D-16 | Partitioning is off at `small` scale and on only for `medium`/`large`. | Avoids many tiny files in dev. | Always partitioned | ✅ |
| D-17 | `fact_service_visit.component_category` added (nullable). | The approved P8 pattern couldn't show up without it. | — (implementation change) | ✅ |
| D-18 | `end_date` 2026-08-31, fictional brands, real EU places, `ground_truth.yaml` gitignored (it depends on seed/preset). | Full FY2025/FY2026 plus partial years. No real companies. | — | ✅ |

## Phase 2 — storage, catalog, refresh (2026-09-21)

| ID | Decision | Why | Instead of | Status |
|---|---|---|---|---|
| D-19 | **Local-only for now.** `StorageConnector` interface + `LocalConnector`. ADLS, the upload helper and the parity test are deferred. | No ADLS account for dev yet. ADLS will be one class plus a registry entry. | Building and mocking ADLS now | ⏸ |
| D-20 | Catalog views are **pinned to the file list** scanned by the last successful refresh. | The data version then describes exactly what queries see. New files stay invisible until a refresh, which keeps caches correct. | Glob views (phase-0 note) | ✅ (replaced the glob-view note) |
| D-21 | A failed validation **rejects** the refresh: the previous version stays active and new fact files stay invisible. `--allow-invalid` activates anyway with `validated = false`. | Safe default with an explicit override. | Snapshotting all data into DuckDB tables | ✅ |
| D-22 | No separate lock file. DuckDB's single-writer lock plus a busy retry (`catalog.busy_timeout_seconds`), and activation re-checks the fingerprint in its transaction. | DuckDB already locks, and releases the lock on a crash. A lock file can go stale. | `catalog.duckdb.lock` | ✅ |
| D-23 | Data version = SHA-256 fingerprint of (uri, size, mtime), plus max fact date. Label `v3 \| 2026-09-21 07:19 UTC \| a1b2c3d4`. | Cheap and deterministic. The label is ASCII because the Windows console mangled `·`. | `·` separators | ✅ |
| D-24 | Post-refresh hooks (`POST_REFRESH_HOOKS`) are the extension point for Phase 4 statistics and Phase 5 cache invalidation. | Refresh doesn't need to know about later phases. | Hard-wiring later phases into refresh | ✅ |
| D-25 | CLI output is ASCII-only. | The Windows cp1252 console crashed on DuckDB box drawing. | DuckDB's pretty printer | ✅ |

## Phase 3 — query execution (2026-09-21)

| ID | Decision | Why | Instead of | Status |
|---|---|---|---|---|
| D-26 | Security boundary = DuckDB sandbox: `allowed_paths` = pinned files, `enable_external_access = false`, `lock_configuration = true`. Statement checks (one `SELECT`, no `PRAGMA`/`CALL`) give clear errors. | Probes showed `read_only` alone still allowed reading any file (including `ground_truth.yaml`) and `COPY … TO`. | `read_only` + a keyword blocklist, or `allowed_directories` (too loose) | ✅ |
| D-27 | Row limit by wrapping: `SELECT * FROM (<sql>) LIMIT max_rows + 1`. `truncated` is a flag, not an error. Default 1000 rows. | Bounded memory, top-N plans. Truncation is normal for listings. | Streaming `fetchmany` | ✅ |
| D-28 | Timeout: run the query on a worker thread and `interrupt()` it from the caller. Default 30 s. | Interrupting from a timer thread during a main-thread fetch **crashes CPython**. | `threading.Timer(con.interrupt)` | ✅ |
| D-29 | `EXPLAIN` before execution (configurable), and `validate()` for Phase 5's validate-and-retry step. | Catches syntax and name errors without reading data. | Execute-and-see | ✅ |
| D-30 | `QueryError` with 14 `kind`s, a cleaned message (caller's line numbers), `hint`, `retryable`, and `to_llm()`. | Designed to feed an LLM retry. | Raw DuckDB exceptions | ✅ |
| D-31 | **One shared sandboxed instance per process.** Concurrent queries use their own cursors on it, and it closes when idle. | DuckDB shares one instance per file per process, and the sandbox settings are instance-wide and locked. | One connection per query (the plan) | ✅ (replaced the plan) |
| D-32 | Per-call `max_rows`/`timeout` can only **lower** the config limits. | A prompt-injected caller can't widen them. | Per-call override | ✅ |
| D-33 | Query log = structured log events (`query_executed` / `query_failed`), persisted via `logging.file`. | The catalog stays read-only for queries. Phase 5 adds per-request logging. | A query-history table | ✅ |
| D-34 | `carq query` replaces `carq catalog sql`. | Only one way to run SQL, and it's the guarded one. | Keeping both | ✅ |
| D-35 | Add the `pytz` dependency. | DuckDB needs it to return `TIMESTAMPTZ` (`now()`). | — | ✅ |
| D-36 | Development is done on one branch per phase (`phase-N-<topic>`), merged into `main` through a PR. | Requested 2026-09-21. Phases 0–2 went straight to `main`. | Committing to `main` | ✅ |
