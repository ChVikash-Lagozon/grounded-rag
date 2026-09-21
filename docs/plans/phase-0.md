# Phase 0 Plan — Project Scaffolding

Status: **approved 2026-09-21 — implemented**

## 1. Environment findings (blockers)

Re-checked on 2026-09-21:

| Tool | State |
|---|---|
| git | 2.55 ✔ (the folder is not yet a git repo). The remote exists and is empty |
| uv | 0.12.17 ✔ |
| Python | 3.12.14 ✔, managed by uv (`.python-version` = 3.12) |

**Location:** moved out of OneDrive to `C:\dev\grounded-rag` (decided 2026-09-21). This avoids syncing `.venv`, the data and the `.duckdb` catalog, and avoids DuckDB file-locking problems.

## 2. Scope of Phase 0

Only scaffolding: no data, no LLM calls, no domain logic.

### Repository layout

```
.
├── pyproject.toml            # uv project, ruff + pytest config
├── uv.lock
├── .python-version
├── .env.example
├── .gitignore
├── CLAUDE.md                 # conventions, commands, "read docs/PROGRESS.md first"
├── README.md                 # skeleton: purpose, setup, commands, layout
├── config/
│   ├── app.yaml              # environment, paths (data_root, catalog path), logging
│   └── local.yaml.example    # optional per-machine overrides (local.yaml is gitignored)
├── src/carquery/
│   ├── __init__.py
│   ├── config.py             # YAML loading + env interpolation + overrides + validation
│   ├── logging.py            # structured logging setup
│   └── cli.py                # `carq` entry point (subcommands added in later phases)
├── data/.gitkeep             # generated data (ignored)
├── eval/reports/.gitkeep
├── docs/
│   ├── PROGRESS.md
│   ├── architecture.md       # Mermaid diagram of the target architecture (skeleton)
│   └── plans/phase-0.md
└── tests/
    ├── conftest.py
    ├── test_config.py
    ├── test_logging.py
    └── test_cli.py
```

Package name: `carquery`, CLI command `carq`. Both are easy to change if you prefer others.

### Config loading (`carquery.config`)

- Load order, later wins: `config/app.yaml` → `config/local.yaml` (optional, gitignored) → environment variables.
- `${ENV_VAR}` and `${ENV_VAR:-default}` interpolation inside YAML. This is how secrets and machine paths get in; YAML never holds secret values.
- Env override convention: `CARQ__SECTION__KEY=value`, e.g. `CARQ__LOGGING__LEVEL=DEBUG`.
- `.env` is loaded through `python-dotenv` if present.
- Validated into typed **pydantic v2** models. A bad config fails fast with a clear message.
- The config directory can be overridden with `CARQ_CONFIG_DIR`, which tests use.
- Secrets are typed `SecretStr`, so they're redacted in logs and in `carq config show`.
- Later phases add their own files (`schema_contract.yaml`, `generator.yaml`, `datasets.yaml`, `ontology.yaml`, `llm.yaml`) through a generic `load_yaml(name, Model)` helper, so this module doesn't need to change.

### Structured logging (`carquery.logging`)

- **structlog** on top of stdlib logging. Output is pretty console in dev and JSON lines when `logging.format: json`, with an optional log file.
- It supports context binding (`request_id`, `phase`, `dataset_version`), which Phase 5's per-request logging needs.

### CLI (`carquery.cli`)

- **typer**. Phase 0 commands are `carq --version`, `carq config show` (resolved config, secrets redacted) and `carq config validate`.

### Dependencies (kept lean)

| Package | Why |
|---|---|
| pydantic | typed, validated config (reused later for the schema contract and LLM I/O) |
| pyyaml | YAML config |
| python-dotenv | `.env` loading |
| structlog | structured / JSON logs with context binding |
| typer | CLI |
| *dev:* pytest, ruff | tests, lint + format |
| *dev (optional):* mypy | type checking. I'd add it but not make it a phase gate yet. Your call |

Heavy deps (duckdb, pyarrow, numpy, streamlit, fastapi, adlfs, openai, anthropic) get added in the phases that need them. UI and API go in optional dependency groups.

### Other files

- `.gitignore`: `.env`, `config/local.yaml`, `.venv/`, `data/**` (except `.gitkeep`), `*.duckdb*`, `eval/reports/*` (except `.gitkeep`), caches.
- `.env.example`: `OPENROUTER_API_KEY`, `AZURE_AI_FOUNDRY_ENDPOINT`, `AZURE_AI_FOUNDRY_API_KEY`, `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `DATA_ROOT`, `CARQ_CONFIG_DIR`, all with placeholder values.
- ruff: line length 100, rules `E,F,I,UP,B,SIM,RUF`, formatter on.

### Tests

- Config loads, and YAML → local → env precedence works.
- Env interpolation works, and a missing required var gives a clear error.
- `CARQ__…` overrides reach nested keys.
- Secrets are redacted in `config show` output.
- JSON logging emits valid JSON with bound context.
- The CLI smoke test (`--version`, `config show`) passes.

### Definition of done

`uv sync` · `uv run pytest` green · `uv run ruff check .` and `ruff format --check .` clean · `uv run carq config show` works · README / CLAUDE.md / PROGRESS.md written · `git init` on `main` and one commit.

## 3. Decisions on the overall brief (answered 2026-09-21)

| # | Topic | Decision | Effect on later phases |
|---|---|---|---|
| 1 | Config vs state | **Split.** `config/datasets.yaml` holds static definitions only. Runtime state lives with the data or catalog, not in git. | Phase 2 |
| 2 | Loading model | **Facts are append-only, dimensions are full reloads.** Loading happens outside this project, so we **don't build a loader**. Files are assumed to be complete at fixed, known paths. We build a **Refresh data** action (CLI `carq refresh`, a Streamlit button, and an API endpoint) that onboards whatever is on disk into DuckDB. | Phases 1, 2, 7 (see below) |
| 3 | Partitioning | **Hive `year=/month=`** for large fact tables (configurable). There's no `load_date` versioning layer. | Phases 1, 2 |
| 4 | Date key | Rename `dim_date.date` → **`calendar_date`**. This is a documented exception to the "identical join key names" rule. | Phases 1, 4 |
| 5 | Recall grain | **Split** into `dim_recall_campaign` + `fact_recall_vehicle`. | Phases 1, 4 |
| 6 | Fiscal label | Apr 2025 – Mar 2026 = **FY2026**. | Phases 1, 4, 6 |
| 7 | LLM providers | **Dev:** OpenRouter free tier, with $10 credit only if needed. **Prod:** an Azure AI Foundry endpoint. | Phases 5, 6 |
| 8 | Few-shot retrieval | BM25/keyword now, behind a `Retriever` interface so an embedding model can plug in later through config. | Phase 5 |
| 9 | Git remote | `origin` = https://github.com/ChVikash-Lagozon/grounded-rag (currently empty). I push only when you say so. | Phase 0 |
| 10 | Provider scope | **Not Anthropic-specific.** Models will be swapped for testing. The code only needs to leave room for provider-native adapters later. | Phase 5 |

### What changes as a result

**Data layout and refresh (decisions 1–3).** The fixed layout under `data_root`, as declared in `config/datasets.yaml`:

```
dim_dealer/dim_dealer.parquet                    # full reload: overwritten in place
fact_sales/year=2026/month=09/<file>.parquet     # append-only: new files are added
fact_warranty_claim/...                          # small facts may be a single file (partitioning is per table)
```

- Catalog views are glob-based (`read_parquet('fact_sales/**/*.parquet', hive_partitioning = true)`), so new files show up without editing any definitions. **[Replaced in Phase 2 by D-20: views are pinned to the file list of the last refresh; see `docs/decisions.md`.]**
- **`carq refresh`** does five things: scan files → validate against the schema contract (PK, FK, nulls, ranges) → rebuild the DuckDB views → recompute context statistics (Phase 4) → record a new **data version** and invalidate the result cache (Phase 5).
- A data version is a fingerprint of the file listing (path, size, mtime) plus the maximum fact date. The UI shows it, and eval reports record it.
- Mutable state (the current data version and the refresh log) goes in a small `_refresh_state` table inside the `.duckdb` catalog, which is not in git.
- **This drops from the brief:** the version folders, the atomic pointer switch, rollback and retention of N versions in Phase 2. The upstream process owns data history.
- **A validation failure can't "not activate" a load**, because the files have already been replaced in place. Instead, a failed refresh keeps the previous data version and statistics, and marks the dataset as *unvalidated* with the errors. The UI then shows a warning. By default, queries still run against the files on disk. The alternative, to snapshot into DuckDB tables so a bad refresh can be fully rejected, gets decided in the Phase 2 plan.
- **Phase 1 generator:** it writes directly into this layout. `generate-history` writes everything. `generate-day N` rewrites the dimensions and **adds** new fact files, which gives a realistic input for testing refresh. Row state changes (a cancelled sale, a claim moving from pending to approved) are not restated in append-only mode. Final status is decided when the row is created, and late-arriving claims arrive as new rows.
- Appending daily files into month folders creates about 30 small files per month. That's fine at `small` scale. Compaction is a Phase 8 topic.

**LLM providers (decisions 7 and 10).**
- There's one generic `LLMProvider` interface. It gets **one implementation now, `OpenAICompatibleProvider`**, which covers OpenRouter (dev) and Azure AI Foundry (prod, through its OpenAI-compatible endpoint). Base URL, auth style (bearer key / `api-key` header / Entra ID token), `api-version` and extra headers are all config. I'll check the exact Foundry endpoint shape when you have one.
- `MockProvider` covers tests. `AnthropicProvider` is **not built**. Native adapters for Anthropic, Gemini and others can be added later as new classes registered by name, with no pipeline changes.
- Free-tier budget: the plan assumes about 50 requests/day. Phase 6 therefore adds a **daily request budget** in config (the run pauses cleanly and resumes the next day), `--tier` / `--subset` flags, and a ~15-question smoke set. Phase 5 makes the table-selection step skippable (send all table descriptions when the schema is small), which saves one call per question.

**`.env.example` changes:** `ANTHROPIC_API_KEY` is replaced by `AZURE_AI_FOUNDRY_ENDPOINT` and `AZURE_AI_FOUNDRY_API_KEY`.

## 3a. Original questions (kept for reference)

1. **`datasets.yaml` holds both config and mutable state.** The brief puts the *active load version* in `config/datasets.yaml`. But that file lives in git, while the loader flips it every day, and on ADLS the active pointer has to live with the data.
   **Proposal:** `config/datasets.yaml` holds only static definitions (logical name, path template, description). The active version and history go in a `_manifest.json` at the storage root, switched atomically (write temp + rename locally, ETag-conditional write on ADLS).
2. **What a "version" means for fact tables.** If every daily load is a full snapshot, the `large` preset (30M+ service visits) would be rewritten every day.
   **Proposal:** Dimensions are full snapshots per load, since they're small. Facts are append-only daily partitions (`load_date=…` holds only that day's rows). A version is then "all fact partitions ≤ load_date + the dimension snapshot for that date", and rollback just moves the pointer.
   This raises a sub-question about state changes on existing rows (a sale later cancelled, a claim moving from pending to approved). I suggest modelling them as restated rows with "latest per key wins" in the view. The other option is to rewrite only the affected partitions.
3. **Hive year/month partitioning combined with `load_date` versioning.** These are two partitioning schemes on the same large facts. I'll propose how they combine in Phase 2. My current leaning is `load_date` for versioning and the year/month layout only for compacted historical data.
4. **The date dimension breaks the "identical join key names" rule.** Facts use `build_date`, `sale_date`, `claim_date` and so on, while `dim_date` uses `date`, which is also a SQL keyword. `dim_date` is a role-playing dimension.
   **Proposal:** Rename the key to `calendar_date` and document an explicit exception to the rule in the contract and ontology.
5. **`fact_recall` grain.** One row per recall × VIN repeats the campaign attributes on every row.
   **Proposal:** Split it into `dim_recall_campaign` (recall_id, campaign_name, component_category, announced_date) and `fact_recall_vehicle` (recall_id, vin, remedy_completed_date).
6. **Fiscal year label.** The fiscal year starts 1 April, but which label does it get? Is Apr 2025–Mar 2026 "FY2026" or "FY2025"? I suggest FY2026 (named by the calendar year it ends in). This matters for golden-question ground truth.
7. **Free-tier budget for evaluation.** Roughly 60 questions × 3 modes × 2–4 LLM calls each comes to about 400–700 calls per run. OpenRouter's free models allow about 50 requests per day without credits, or about 1,000 per day with ≥$10 credit.
   Which development model and quota should I plan for? Resumable runs are already in scope, but a full comparison may take several days on the lowest tier.
8. **Few-shot retrieval.** I'll use keyword/BM25-style similarity, which needs no dependencies. Embeddings are an optional, config-selected capability, so the core install doesn't pull in an embedding model.
9. **Git remote.** Should the repo be local only, or will you provide a remote (GitHub / Azure DevOps)? I will not push anything without asking.
10. **Microsoft Foundry.** Just confirming that the `AnthropicProvider` Foundry path only needs to be built and unit-tested (with mocks) now. Live testing would come when you have an endpoint.

## 4. What I need from you

- ~~Approval to install uv + Python 3.12~~ (already installed).
- ~~OneDrive location~~ → `C:\dev\grounded-rag`.
- ~~Names~~ → keep `carquery` / `carq`.
- ~~Views on Section 3~~ → recorded above.
- **Remaining: approval to implement Phase 0.**
