# grounded-rag (`carquery`)

Ask business questions in plain English about a car manufacturer's data. An LLM turns each
question into DuckDB SQL, which runs against parquet files. The LLM never sees bulk data, only
the schema, descriptions, small samples, statistics and (optionally) a business ontology.

> Status: **Phase 0 (scaffolding) done.** See [docs/PROGRESS.md](docs/PROGRESS.md) for what
> exists today and [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full plan.

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

## Layout

```text
config/        YAML configuration (app.yaml now; contract, datasets, ontology, llm later)
src/carquery/  package: config, logging, CLI (more modules per phase)
data/          generated/onboarded data and the DuckDB catalog (gitignored)
eval/          golden questions, few-shot library, reports (reports gitignored)
docs/          PROGRESS.md, architecture.md, plans/phase-N.md
tests/         pytest suite
```

## Architecture

See [docs/architecture.md](docs/architecture.md).
