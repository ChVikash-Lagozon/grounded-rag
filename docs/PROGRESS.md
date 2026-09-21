# Progress

| Phase | Status |
|---|---|
| 0 — Scaffolding | ✅ done (2026-09-21) |
| 1 — Schema contract, synthetic generator, validation | ✅ done (2026-09-21) |
| 2 — Storage connectors, catalog, refresh | next: plan to be written |
| 3 — Query execution layer | — |
| 4 — Context builders and ontology | — |
| 5 — LLM provider layer and orchestration | — |
| 6 — Evaluation harness | — |
| 7 — Interface (Streamlit + FastAPI) | — |
| 8 — Performance / scale-up | later, plan on request |

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
