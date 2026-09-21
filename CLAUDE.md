# CLAUDE.md

Natural-language → DuckDB SQL over parquet (car manufacturer PoC). Package `carquery`, CLI `carq`.

## Start of every session

1. Read `docs/PROGRESS.md` (current phase, what exists, known limitations).
2. The full requirements are in `PROJECT_BRIEF.md`, and decisions that override it are in
   `docs/plans/phase-0.md` §3.

## Working rules

- Work **phase by phase**. Before implementing phase N, write `docs/plans/phase-N.md` and
  **stop for approval**.
- A phase is done when: `uv run pytest` is green, `uv run ruff check .` and
  `uv run ruff format --check .` are clean, README is updated, and a `docs/PROGRESS.md` entry is
  written. Then commit.
- Never commit secrets, `.env`, `config/local.yaml` or generated data. Never push without asking.
- Nothing hardcoded: no paths, credentials, provider URLs or model names in code. They belong
  in `config/*.yaml` and env vars. Add new env vars to `.env.example`.
- Python 3.11+, type hints throughout, keep dependencies lean (justify heavy ones in the plan).
- Raise ambiguities in the phase plan instead of guessing.

## Commands

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run carq config show | validate
```

## Conventions

- Config: `carquery.config.load_config()` for `app.yaml`. New config files use
  `load_yaml("<file>.yaml", PydanticModel)`, which gives `${VAR:-default}` interpolation and
  validation. Models use `extra="forbid"`. Secrets are `SecretStr`.
  Env overrides are `CARQ__SECTION__KEY`.
- Logging: `from carquery.logging import get_logger, bound_context`. Log events as snake_case
  names with key/value fields (`log.info("query_executed", rows=10)`), not f-strings.
- Tests: use the `config_dir` fixture (a temporary config dir) and pass `env={...}` explicitly,
  so tests never depend on the machine's environment.
- The CLI is typer. Add subcommands in `carquery/cli.py`, or as sub-apps once it grows.

## Key decisions (see docs/plans/phase-0.md §3)

- Loading is **external**. Dimensions are full reloads at fixed paths, and facts are
  append-only files in `year=/month=` partitions. We only build **refresh**: scan → validate →
  rebuild views → recompute stats → new data version → invalidate cache. There are no version
  folders and no rollback.
- `config/datasets.yaml` = static definitions. Runtime state (data version, refresh log) is kept
  in the `.duckdb` catalog.
- `dim_date` key = `calendar_date`. Recall is split into `dim_recall_campaign` and
  `fact_recall_vehicle`. The fiscal year starts 1 April; Apr 2025–Mar 2026 = **FY2026**.
- LLM: **provider-agnostic**. `OpenAICompatibleProvider` covers OpenRouter (dev, free tier)
  and Azure AI Foundry (prod). `MockProvider` is for tests. Native adapters can be added later.
  Assume about 50 requests/day in dev.
- Few-shot retrieval: BM25/keyword behind a `Retriever` interface, with embeddings pluggable
  later.
- Remote: `origin` = github.com/ChVikash-Lagozon/grounded-rag. Push only when asked.
