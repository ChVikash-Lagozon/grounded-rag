# grounded-rag (`carquery`)

Ask business questions in plain English about a car manufacturer's data. An LLM turns each
question into DuckDB SQL, which runs against parquet files. The LLM never sees bulk data, only
the schema, descriptions, small samples, statistics and (optionally) a business ontology.

> Status: **Phase 1 (schema contract, synthetic data, validation) done.** See
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
uv run carq validate               # check data/ against the schema contract (exit 1 on errors)
uv run carq profile                # regenerate docs/data_profile.md
uv run carq schema erd             # regenerate docs/schema.md (ER diagram) from the contract

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

## Layout

```text
config/        YAML configuration: app, schema contract, generator + reference catalogue
src/carquery/  package: config, logging, CLI, contract, generator/, validation, profile
data/          generated/onboarded data and the DuckDB catalog (gitignored)
eval/          golden questions, few-shot library, reports (reports gitignored)
docs/          PROGRESS.md, architecture.md, schema.md, data_profile.md, plans/phase-N.md
tests/         pytest suite
```

## Architecture

See [docs/architecture.md](docs/architecture.md).
