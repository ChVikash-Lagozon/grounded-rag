# Progress

| Phase | Status |
|---|---|
| 0 — Scaffolding | ✅ done (2026-09-21) |
| 1 — Schema contract, synthetic generator, validation | ✅ done (2026-09-21) |
| 2 — Storage connectors, catalog, refresh | ✅ done (2026-09-21), local-only (ADLS deferred) |
| 3 — Query execution layer | next: plan to be written |
| 4 — Context builders and ontology | — |
| 5 — LLM provider layer and orchestration | — |
| 6 — Evaluation harness | — |
| 7 — Interface (Streamlit + FastAPI) | — |
| 8 — Performance / scale-up | later, plan on request |

## Phase 2 — Storage connector, catalog, refresh (2026-09-21)

**What was built**

- **Datasets** `config/datasets.yaml` + `carquery.datasets`: one dataset per contract table
  (path + glob pattern, per-table overrides). Tables unknown to the contract and paths
  escaping the storage root are config errors. Descriptions stay in the contract.
- **Storage** `carquery.storage`: the `StorageConnector` protocol (`configure`, `list_files`,
  `describe`), `LocalConnector`, and `get_connector()` keyed by `storage.backend`.
- **Catalog** `carquery.catalog`: the persistent `paths.catalog_path` DuckDB file. It has one
  view per table (contract columns only, pinned to an explicit file list) and the state tables
  `_data_version` (highest id = active), `_refresh_log`, `_table_stats` (rows, files, bytes,
  date range) and `_files`. `open_catalog(config)` gives read-only query connections.
  `connect_catalog` retries while another process writes (`catalog.busy_timeout_seconds`),
  and recognises both the POSIX lock and the Windows sharing-violation errors.
- **Refresh** `carquery.refresh.refresh()`: scan → fingerprint (SHA-256 of uri/size/mtime) →
  `no_change` if the fingerprint is the same → validate the scanned files on a scratch
  connection → `rejected` on errors (unless `allow_invalid`, which marks the version
  `validated = false`) → activate in one transaction that re-checks the fingerprint → post-refresh
  hooks (`POST_REFRESH_HOOKS`, empty for now). Failures give `status="error"` and a log row
  instead of an exception.
- **Validation** was refactored into `validate_views(con, contract)`, so it runs on any
  connection with views. `validate(data_root, …)` is still the local shortcut.
- **CLI**: `carq refresh [--force] [--allow-invalid]`, `carq catalog status`,
  `carq catalog sql "…" [--limit]`, `carq storage check`. `carq validate` now checks what the
  connector sees. CLI output is ASCII-only, because the Windows console could not print
  DuckDB's box drawing or `·`.

**Changes from the approved plan**

- Local-only, as decided at approval (§0): no `ADLSConnector`, `carq storage upload`,
  ADLS config/env vars, or parity test.
- There is no lock file (§0). DuckDB's own single-writer lock plus retry is used instead.
- The version label is `v3 | 2026-09-21 07:19 UTC | a1b2c3d4` (`|` instead of `·`, and the
  time is marked UTC).
- `refresh_id` is a random 12-character hex id, not a sequence, so it can be bound to logs
  before the catalog is written.
- New `catalog:` config section: `busy_timeout_seconds`, `history_limit`.

**How to run**

```bash
uv run carq storage check
uv run carq refresh            # v1
uv run carq generate day 1 && uv run carq refresh   # v2
uv run carq catalog status
uv run carq catalog sql "SELECT count(*) FROM fact_sales"
```

**Known limitations**

- Dimensions are overwritten in place, so views read new dimension contents right away,
  even before a refresh (or after a rejected one). Only fact files are really pinned.
- A refresh opens the catalog for writing briefly. A long-lived read-only connection in
  another process (for example a future Streamlit app) blocks it until the timeout.
  Readers should keep connections short (Phase 7).
- `catalog sql` has no guards, limits or timeouts beyond read-only access. That is Phase 3.
- Views list files explicitly with `hive_partitioning = false`, so date filters don't prune
  partitions (Phase 8).
- `_table_stats` has only counts and date ranges. Richer statistics come in Phase 4 through the
  hook.

**Next steps**

- Write `docs/plans/phase-3.md`: the query execution layer (read-only SQL guard, limits and
  timeouts, result shaping) on top of `open_catalog`.

## Phase 1 — Schema contract, synthetic generator, validation (2026-09-21)

**What was built**

- **Schema contract** `config/schema_contract.yaml`: 8 dimensions and 7 facts, with types,
  nullability, primary and foreign keys, allowed values, ranges, row-level checks and a
  description for every table and column. `carquery.contract` loads it (pydantic) and gives
  arrow schemas, a Mermaid ER diagram and the markdown schema doc.
- **Generator** `carquery.generator`: numpy-vectorised, seeded, and chunked by build month.
  - It simulates one deterministic timeline (history start → `end_date + day_horizon_days`).
    `generate history` writes rows that arrived by `end_date`. `generate day N` writes only the
    rows arriving on `end_date + N` and rewrites the dimensions as of that day.
  - Final files are written by DuckDB `COPY` from a staging area: contract types, ZSTD, sorted
    by date column, preset row-group size, optional `year=/month=` partitions.
  - Presets: `test` (15k vehicles), `small` (50k, ~540k rows, ~3 s), `medium`/`large`
    (defined, not run).
  - Messiness: nulls, cancelled sales with resales, rejected and pending claims,
    late-arriving claims (`received_date`), closed and newly opened dealers, unsold
    inventory.
  - Planted patterns P1–P8 (brake batch + recall, BEV growth by region, seasonality +
    fiscal quarter-end, slow dealers, line change, fleet trade-off, battery wear-out by
    supplier, regional suspension repairs). Parameters and detection SQL are in
    `data/ground_truth.yaml`.
- **Validation** `carquery.validation`: files present, schema, primary key, not-null, allowed
  values, min/max, row checks and foreign keys, all as DuckDB SQL. It runs after every
  generation and via `carq validate`.
- **Profile** `carq profile` → `docs/data_profile.md`. **ER diagram** `carq schema erd` →
  `docs/schema.md` (a test fails if it's stale).
- `carquery.datafiles`: the parquet layout and a DuckDB connection with one view per table.
  Phase 2 builds the catalog on top of it.

**Changes from the approved plan**

- `fact_service_visit` gained a nullable `component_category` (NULL for scheduled visits).
  Without it the approved P8 "suspension repair visits" pattern could not show up.
- Recall campaigns: 2 (the P1 brake recall plus one infotainment software recall from config).
- The contract has an `arrival_column` field (set for `fact_warranty_claim.received_date`).
- `paths.docs_dir` was added to `app.yaml` for the generated docs.
- Determinism is tested as byte-identical parquet files, as planned.

**How to run**

```bash
uv run carq generate history [--preset small] [--seed 42] [--overwrite]
uv run carq generate day 1
uv run carq validate
uv run carq profile && uv run carq schema erd
uv run pytest            # ~80 tests, ~30 s (generates the `test` preset once per session)
```

**Known limitations**

- Row state is fixed when a row is written (claim `Pending`, `remedy_completed_date`
  NULL). Later day files don't restate it, because merging is upstream's job (Phase 1
  decision 2). So a claim that was `Pending` in the history stays `Pending` in the files.
- `generate day N` re-simulates the whole timeline each time. That takes seconds at `small`
  scale, but medium/large would need a faster path (Phase 8).
- The history starts "cold": service and claim volumes ramp up in the first year. Use rates,
  not raw counts, when comparing years.
- If validation fails after generation, the command exits 1, but the files have already been
  written. Phase 2's refresh decides what happens with unvalidated data.
- `medium`/`large` presets are not run yet. `dim_customer` (individuals) is held in memory,
  which is fine up to `large`.
- New trims only appear with a new model year. Daily dimension changes are only new or
  closed dealers.

**Next steps**

- Write `docs/plans/phase-2.md`: `StorageConnector` (Local, ADLS), static
  `config/datasets.yaml`, the persistent `.duckdb` catalog with views over the fixed-path
  layout, and `carq refresh` (scan → validate → rebuild views → data version in
  `_refresh_state`).

## Phase 0 — Scaffolding (2026-09-21)

**What was built**

- A uv project (`pyproject.toml`, `uv.lock`, Python 3.12), with ruff and pytest configured.
- `carquery.config`: loads YAML → `local.yaml` → `CARQ__…` env overrides, supports
  `${VAR:-default}` interpolation and `.env` loading, validates with pydantic (unknown keys
  rejected), resolves paths against the repo root, and redacts secrets. `load_yaml()` is
  provided for the config files later phases add.
- `carquery.logging`: structlog on top of stdlib, with console or JSON output, an optional JSON
  log file, and context binding (`bind_context`, `bound_context`). Third-party stdlib loggers go
  through the same formatting.
- `carq` CLI: `--version`, `config show [--format yaml|json]`, `config validate`.
- `.env.example`, `.gitignore`, `CLAUDE.md`, a README skeleton and `docs/architecture.md`.
- Brief-level decisions are recorded in `docs/plans/phase-0.md` §3.

**How to run**

```bash
uv sync
uv run pytest
uv run carq config show
```

**Known limitations**

- `local.yaml` and the `CARQ__` overrides apply only to `app.yaml`. Other config files get
  interpolation only. That's intentional for now; revisit if needed.
- mypy is not set up yet (optional in the plan).

**Next steps**

- Write `docs/plans/phase-1.md`: final column lists for the schema contract, the extra planted
  patterns, generator design (vectorised, chunked, with scale presets), the fixed-path output
  layout, and the validation module.
