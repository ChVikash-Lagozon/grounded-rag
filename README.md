# grounded-rag (`carquery`)

Ask business questions in plain English about a car manufacturer's data. An LLM turns each
question into DuckDB SQL, which runs against parquet files. The LLM never sees bulk data, only
the schema, descriptions, small samples, statistics and (optionally) a business ontology.

> Status: **Phase 3 (guarded query execution) done.** See
> [docs/PROGRESS.md](docs/PROGRESS.md) for what exists today and
> [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full plan.

## Setup

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for the project automatically.

```bash
uv sync                    # create .venv and install dependencies
cp .env.example .env       # then fill in secrets (never commit .env)
```

Keep the repo **outside OneDrive or other synced folders**, because DuckDB files and `.venv`
don't work well with sync.

## Commands

```bash
uv run carq --version
uv run carq config show            # resolved config, secrets redacted (--format json also works)
uv run carq config validate        # exit code 1 if the config is invalid

uv run carq generate history       # synthetic history into data/ (--preset, --seed, --overwrite)
uv run carq generate day 1         # append the files for the day after the history
uv run carq validate               # check the storage files against the contract (exit 1 on errors)
uv run carq profile                # regenerate docs/data_profile.md
uv run carq schema erd             # regenerate docs/schema.md (ER diagram) from the contract

uv run carq storage check          # configured storage and its files per dataset
uv run carq refresh                # scan, validate, activate a new data version (--force, --allow-invalid)
uv run carq catalog status         # active version, data-through date, latest refreshes
uv run carq query "SELECT count(*) FROM fact_sales"   # guarded query (--max-rows, --timeout,
                                                     #   --explain, --format table|json)

uv run pytest                      # tests
uv run ruff check .                # lint
uv run ruff format .               # format
```

## Configuration

| Layer (later wins) | Where | In git? |
|---|---|---|
| Base | `config/app.yaml` | yes |
| Per machine | `config/local.yaml` (copy from `local.yaml.example`) | no |
| Environment | `CARQ__SECTION__KEY=value`, e.g. `CARQ__LOGGING__LEVEL=DEBUG` | no |

- YAML values can reference the environment: `${VAR}` (required) or `${VAR:-default}`.
- `.env` in the repo root is loaded automatically. Real environment variables take precedence.
- Relative paths resolve against the repo root.
- `CARQ_CONFIG_DIR` points at a different config directory.
- Secrets are never stored in YAML. They come from the environment and are redacted in output.

## Data

The data model is a star schema of 8 dimensions and 7 facts, defined once in
`config/schema_contract.yaml`. See [docs/schema.md](docs/schema.md) for the ER diagram and every
column. GitHub and the VS Code Markdown preview (with the Mermaid extension) render the diagram,
or paste it into https://mermaid.live to zoom and export it.

For now the data is fully synthetic, from a seeded generator (`config/generator.yaml`, with
the catalogue in `config/synthetic_reference.yaml`):

| Preset | Vehicles | Use |
|---|---|---|
| `test` | 15k | pytest |
| `small` | 50k, 3 years (~540k rows, a few seconds) | default |
| `medium` / `large` | 1M / 5M | supported by design, not run yet |

- **Layout** under `data_root`: `<table>/<table>.parquet` for dimensions (fully rewritten),
  and `<table>/<table>_history.parquet` plus `<table>_<date>.parquet` per daily increment for
  facts. The medium/large presets partition big facts as `<table>/year=YYYY/month=MM/`. Files
  are ZSTD-compressed and sorted by the table's date column.
- **Planted patterns** (8 of them, e.g. a defective brake batch followed by a recall, BEV
  growth by region, slow dealers) are documented with detection SQL in
  `data/ground_truth.yaml`, which is written with the data.
- **Validation** checks schema, primary keys, not-null columns, allowed values, ranges, row
  checks and foreign keys. It runs after every generation.
- [docs/data_profile.md](docs/data_profile.md) contains row counts, null rates and
  distributions.

## Catalog and refresh

Queries run on a persistent DuckDB **catalog** (`paths.catalog_path`, default
`data/catalog.duckdb`) with one view per table. Loading data is external: dimensions are
rewritten in place, facts get new files. The catalog only changes when you run
`carq refresh`:

1. **Scan** the storage connector (`storage.backend`; only `local` so far) for each dataset
   in `config/datasets.yaml` (paths and glob patterns, one per contract table).
2. **Fingerprint** the file list (uri, size, modified). Unchanged files: `no_change`.
3. **Validate** the scanned files against the schema contract.
4. **Reject** on errors (the previous version stays active and the new files stay invisible),
   unless `--allow-invalid`, which activates the version marked "not validated".
5. **Activate** in one transaction: views pinned to the exact scanned files, per-table stats,
   and a new data version such as `v3 | 2026-09-21 07:19 UTC | d89ebdc8`.

Every attempt is logged in the catalog (`carq catalog status`). DuckDB allows one writing
process per file, so a refresh waits up to `catalog.busy_timeout_seconds` if another process is
writing. Note that dimension files overwritten in place are read by the views immediately,
even before a refresh.

## Querying

`carq query` and `carquery.query.QueryEngine` run one SQL statement (DuckDB dialect) on the
catalog. This is the layer the LLM pipeline will use:

- **Only reads.** One statement typed `SELECT` (including `WITH`, `FROM x`, `DESCRIBE`,
  `SUMMARIZE`, `SHOW`). `PRAGMA`, `CALL`, DDL/DML, `COPY`, `ATTACH`, `SET` … are rejected.
- **Sandboxed.** The connection may only read the files pinned by the last refresh, and the
  configuration is locked. Other files (including `data/ground_truth.yaml`), extensions and
  settings can't be accessed.
- **Limited.** `query.max_rows` (more rows set `truncated`), `query.timeout_seconds` (the
  query is cancelled), and `query.memory_limit`/`threads`. A caller can lower these per call
  but never raise them.
- **Validated first.** `EXPLAIN` catches syntax, unknown tables and unknown columns without
  reading data. `engine.validate(sql)` runs only this step.
- **Structured errors.** `QueryError` has a `kind` (`syntax`, `unknown_table`,
  `unknown_column`, `timeout`, `forbidden`, …), the DuckDB message (with line pointer and
  "did you mean"), a `hint`, and `retryable`. `to_llm()` renders it for a retry prompt.
- **Logged** as `query_executed` / `query_failed` events with SQL, duration, rows and data
  version. Set `logging.file` to keep them as JSON lines.

## Layout

```text
config/        YAML configuration: app, schema contract, datasets, generator + reference
src/carquery/  package: config, logging, CLI, contract, generator/, validation, profile,
               datasets, storage, catalog, refresh, query
data/          generated/onboarded data and the DuckDB catalog (gitignored)
eval/          golden questions, few-shot library, reports (reports gitignored)
docs/          PROGRESS.md, architecture.md, schema.md, data_profile.md, plans/phase-N.md
tests/         pytest suite
```

## Architecture and project knowledge

- [docs/architecture.md](docs/architecture.md): diagram, component status, the query path as built
- [docs/PROGRESS.md](docs/PROGRESS.md): what each phase delivered, limitations, next steps
- [docs/decisions.md](docs/decisions.md): every design decision, why, and what it replaced
- [docs/findings.md](docs/findings.md): verified DuckDB behaviour, bugs caught, lessons, open risks
- [docs/plans/](docs/plans/): the approved plan per phase, with implementation changes
