# Project Brief: Natural-Language Query Interface over Parquet (Car Manufacturer PoC)

## 1. Goal

Build a cost-optimised proof of concept that lets users ask business questions in plain English about a car manufacturer's data. An LLM translates each question into SQL, which runs against parquet files through DuckDB, and the result is returned (optionally with a short natural-language summary).

Key principles:

- **No transactional database.** Data lives in parquet files. Data is refreshed in **daily batches** and is read-only between refreshes.
- **The LLM never receives bulk data.** It only sees schema, descriptions, small samples, column statistics and (optionally) a business ontology.
- **Everything is swappable via config:** storage backend, LLM provider/model, context mode, data source (synthetic now, real data later).
- **Local first.** Everything must run on a laptop. ADLS is the second storage target; S3/GCS/MinIO/on-prem come later as new connectors without changes to core code.
- **Measurable.** We compare answer quality with and without a business ontology using an evaluation harness.

## 2. How we work (rules for the coding agent)

- Work strictly **phase by phase** (Section 6). At the start of each phase, write a plan to `docs/plans/phase-N.md` (approach, files to create, schemas/interfaces, open questions) and **STOP and wait for my approval** before implementing.
- Each phase ends with: code runnable, tests passing (`pytest`), lint clean (`ruff`), README updated, and a short summary in `docs/PROGRESS.md` (what was built, how to run it, known limitations, next steps).
- In Phase 0, create a `CLAUDE.md` in the repo root with project conventions and commands, and keep it current so future sessions can pick up where we left off. Always read `docs/PROGRESS.md` at the start of a session.
- Use git. One commit (or a few logical commits) per phase, with clear messages. Never commit secrets or generated data.
- Python 3.11+, dependency management with `uv` (fall back to venv + requirements.txt if unavailable). Type hints throughout. Keep dependencies lean; justify any heavy dependency in the plan.
- All configuration via YAML files in `config/` plus environment variables for secrets. Provide `.env.example`. **No hardcoded paths, credentials, provider URLs or model names in code.**
- Prefer simple, readable code over clever abstractions, but respect the interfaces defined here so components can be swapped.
- If a requirement here is ambiguous or seems wrong, raise it in the phase plan rather than guessing silently.

## 3. Target architecture

```
User question
   │
   ▼
UI (Streamlit) / API (FastAPI)
   │
   ▼
Orchestrator ──► LLM Provider (generic interface; OpenRouter first)
   │   1. table selection
   │   2. SQL generation (with context from Context Builder)
   │   3. validate (EXPLAIN) → retry with error (max 2)
   │   4. execute
   │   5. optional summary
   ▼
Query Engine (DuckDB, read-only, limits, timeouts)
   │
   ▼
Catalog (DuckDB views registered from manifest)
   │
   ▼
Storage Connector (Local | ADLS | future: S3, GCS, MinIO...)
   │
   ▼
Versioned parquet datasets  ◄── Loader ◄── Data Source (Synthetic generator now | real data later)
                                              │
                                     validated against Schema Contract
```

## 4. Data model and schema contract

### 4.1 Schema contract

Define a single **schema contract** (e.g. `config/schema_contract.yaml`, loaded into pyarrow schemas) describing every table: columns, types, nullability, primary key, foreign keys, and a human-readable description for each table and column. This contract is the source of truth for:

- the synthetic generator (must produce exactly this),
- any future real-data loader (must conform to this),
- data quality validation on every load,
- the schema context sent to the LLM.

### 4.2 Tables (star schema)

Propose final column lists in the Phase 1 plan; the following is the starting point.

Dimensions:

- `dim_date`: date, year, quarter, month, month_name, week_of_year, day_of_week, is_weekend, fiscal_year, fiscal_quarter (fiscal year starts 1 April — deliberately different from calendar year).
- `dim_vehicle_model`: model_id, brand, model_name, trim, body_type, segment, powertrain (ICE / Hybrid / PHEV / BEV), engine_displacement_l, battery_kwh, range_km, fuel_consumption_l_per_100km, first_model_year, last_model_year, base_msrp.
- `dim_plant`: plant_id, plant_name, country, city, region, daily_capacity, opened_year.
- `dim_dealer`: dealer_id, dealer_name, country, city, region, dealer_tier, opened_date, is_active.
- `dim_customer`: customer_id, customer_type (individual / fleet), age_band, country, city, region, first_purchase_date. No realistic personal names or PII.
- `dim_supplier`: supplier_id, supplier_name, country, supplier_tier, quality_rating.
- `dim_part`: part_id, part_name, component_category (brakes, powertrain, battery, electrical, infotainment, suspension, body), unit_cost, primary_supplier_id.

Facts:

- `fact_production`: vin, model_id, plant_id, build_date, model_year, exterior_colour, production_cost, passed_first_inspection, rework_hours.
- `fact_part_supply`: supply_batch_id, part_id, supplier_id, plant_id, delivery_date, quantity, unit_cost, inspected_defect_rate.
- `fact_vehicle_component`: vin, part_id, supply_batch_id (links each vehicle to the batches of its key tracked components — a limited set of ~5–8 key components per vehicle, not a full bill of materials).
- `fact_sales`: sale_id, vin, dealer_id, customer_id, sale_date, dealer_arrival_date, sale_type (retail / fleet / lease), list_price, discount_amount, net_price, is_cancelled.
- `fact_service_visit`: visit_id, vin, dealer_id, visit_date, visit_type (scheduled / repair / recall), odometer_km, labour_hours, total_cost, is_warranty.
- `fact_warranty_claim`: claim_id, vin, dealer_id, part_id, claim_date, failure_mode, odometer_km, labour_cost, parts_cost, total_cost, claim_status (approved / rejected / pending).
- `fact_recall`: recall_id, campaign_name, vin, component_category, announced_date, remedy_completed_date (null if not yet remedied).

### 4.3 Modelling rules

- snake_case, descriptive names; join keys have **identical names** in every table.
- Proper types: DATE for dates, DECIMAL for money, BOOLEAN for flags, readable categorical values (`'Approved'`, not `'A'`).
- Currency: a single currency (EUR) stated in column descriptions.
- Referential integrity must hold (every FK resolves), except where deliberately modelled messiness is documented.

## 5. Synthetic data — requirements

We start with **fully synthetic data at small scale**. It will later be replaced with a larger or real-like dataset, so the generator must be a pluggable **data source** that conforms to the schema contract, not something the rest of the system depends on.

- **Reproducible:** seeded; same seed + config = identical output.
- **Scale presets** in `config/generator.yaml`: `small` (~50k vehicles built, ~3 years of history) — the one we use now; `medium` (~1M vehicles) and `large` (~5M vehicles, 30M+ service visits) must be supported by design (chunked / vectorised generation or DuckDB SQL generation, memory-safe on a laptop) but will not be run yet.
- **History window:** configurable (default 3 years ending at a configurable end date).
- **Realistic distributions:** model mix, powertrain mix, regional differences, price/discount variation, service intervals by mileage, failure rates by component and vehicle age, fleet vs retail behaviour.
- **Realistic messiness:** some nulls in non-key columns, late-arriving warranty claims, cancelled sales, rejected claims, a few dealers that closed.
- **Planted patterns** — non-obvious, only discoverable by querying (joins/aggregations), each documented in `data/ground_truth.yaml` with exact parameters:
  1. A specific supplier batch of a brake component causes elevated warranty claims, but only for vehicles built at one plant during a ~6-week window; a recall campaign follows a few months later.
  2. BEV share of sales grows steadily over the history window, at noticeably different rates per region.
  3. Seasonal sales patterns plus quarter-end spikes (by fiscal quarter).
  4. A small group of dealers consistently has much longer days-to-sell (sale_date − dealer_arrival_date).
  5. One plant has a lower first-inspection pass rate for one model after a specific date (e.g. a line change).
  6. Fleet customers receive higher discounts but generate more service visits per vehicle.
  7. At least two more patterns of your own design (propose them in the plan).
- **Daily batch mode:** the generator can produce "day N" incremental data (new production, sales, services, claims, late-arriving claims for earlier vehicles). Dimensions change slowly (occasional new dealer, new model year trims).
- **Parquet writing:** ZSTD compression; sensible row group sizes; fact tables sorted by their main date column; Hive partitioning by year/month only for large fact tables (configurable, off for small tables). Avoid many tiny files.
- **Data quality validation** after every load: schema matches contract, PK uniqueness, FK integrity, non-null constraints, value ranges. Loads failing validation must not be activated.

## 6. Phases

### Phase 0 — Project scaffolding
Repo structure (`src/`, `config/`, `data/`, `eval/`, `docs/`, `tests/`), uv project, ruff + pytest config, config loading (YAML + env), structured logging, `.env.example`, `.gitignore` (excluding `data/` outputs and `.env`), `CLAUDE.md`, `docs/PROGRESS.md`, README skeleton.

### Phase 1 — Schema contract, synthetic generator, validation
Everything in Sections 4 and 5, run at `small` scale. Deliverables: CLI command to generate the full history, CLI command to generate day N increments, `data/ground_truth.yaml`, validation module, a data profile report (row counts, null rates, basic distributions) in `docs/data_profile.md`, and tests that verify the planted patterns are actually detectable with SQL.

### Phase 2 — Storage connectors, manifest, catalog, versioned refresh
- `StorageConnector` interface: list datasets, resolve URIs, configure engine credentials. Implement `LocalConnector` and `ADLSConnector` (DuckDB azure extension and/or fsspec/adlfs; support connection string, service principal and managed identity).
- `config/datasets.yaml` manifest: logical table name → path, description, active load version.
- Loader writes each daily load to a new version folder (e.g. `<table>/load_date=YYYY-MM-DD/`), validates it, then atomically switches the active version in the manifest. Support rollback to the previous version and retention of the last N versions.
- Catalog: registers each active dataset as a DuckDB view in a persistent `.duckdb` file; rebuilt on refresh.
- Switching Local → ADLS must be a config change only. Include a test that runs identical queries against both (ADLS test skipped when credentials are absent).
- Optional local file cache for remote storage.

### Phase 3 — Query execution layer
Read-only enforcement (only SELECT / WITH), configurable row limit and timeout, EXPLAIN-based validation before execution, structured error objects suitable for feeding back to an LLM, query logging (SQL, duration, rows).

### Phase 4 — Context builders and ontology
- Common `ContextBuilder` interface with three modes selected by config:
  - `schema_only`: table/column names, types, a few sample rows, basic stats (row counts, distinct values for low-cardinality columns, min/max dates).
  - `schema_with_descriptions`: the above plus table/column descriptions from the schema contract.
  - `ontology`: the above plus `config/ontology.yaml`.
- Write `config/ontology.yaml` for this domain: entities mapped to tables; relationships and canonical join paths; synonyms (retailer → dealer, variant → trim, turnover → revenue, EV → BEV, etc.); metric definitions (units sold, net revenue, discount rate, days-to-sell, warranty cost per vehicle, claim rate per 1,000 vehicles, first-inspection pass rate, BEV share); disambiguation rules ("sales" means units unless revenue is stated; model year vs build year vs sale year; fiscal vs calendar year; which region — plant, dealer or customer); value hierarchies (region → country → city).
- Context output must be deterministic and stable (to benefit from prompt caching on providers that support it).
- Context statistics are recomputed on each data refresh.

### Phase 5 — LLM provider layer and orchestration
- **Generic provider interface.** Implementations:
  - `OpenAICompatibleProvider`: configurable base_url, api key env var, model id, extra headers. This covers OpenRouter (our starting provider, base URL `https://openrouter.ai/api/v1`), Ollama, Groq, Gemini and others.
  - `AnthropicProvider`: supports both Microsoft Foundry and direct Anthropic API endpoints (for production later).
  - `MockProvider`: canned responses for deterministic tests.
- Provider and model per pipeline step are configurable (e.g. a cheaper model for table selection, a stronger one for SQL generation).
- Provider-specific features (prompt caching, batch API) are **optional capabilities**, detected from config and used when available, never required by the core pipeline.
- Robustness: exponential backoff on 429/5xx, configurable requests-per-minute throttle, clear error when a daily free-tier quota is exhausted.
- Pipeline: (1) table selection from short table descriptions; (2) SQL generation with context for selected tables; (3) EXPLAIN validation; (4) on failure, retry up to 2 times with the error message; (5) execute; (6) optional natural-language summary.
- All prompts explicitly require **DuckDB SQL dialect**. Keep prompts clear and model-agnostic; don't over-tune them to the development model.
- Few-shot library (`eval/fewshot.yaml`) of verified question → SQL pairs; retrieve up to 3 similar examples per question (simple keyword/embedding similarity; keep it lightweight).
- Per-request logging: provider, model, context mode, tables selected, SQL, retries, latency per step, input/output/cached tokens, estimated cost (pricing per model in config; free models = 0).
- Result cache for repeated questions, invalidated on data refresh.

### Phase 6 — Evaluation harness
- `eval/golden_questions.yaml` with ~60 questions in tiers: simple lookups, aggregations, multi-table joins, time-based analysis, ambiguous business terms (to test the ontology), and questions targeting each planted pattern.
- Each question has **reference SQL**, not hardcoded answers: expected results are recomputed from the reference SQL against the current dataset, so the golden set stays valid when data is regenerated or scaled up. Prefer questions whose meaning is scale-invariant (rates, rankings, shares, trends).
- Harness runs all questions in each context mode and compares result sets (order-insensitive where appropriate, tolerant numeric comparison, column-name agnostic).
- Resumable runs (important with free-tier rate limits), configurable concurrency.
- Reports (markdown + CSV) in `eval/reports/`: execution accuracy per mode and per tier, retry rate, mean/p95 latency, tokens and cost per question. Every report records provider, model and dataset version; results from different models are never mixed in one comparison.

### Phase 7 — Interface
- Streamlit UI: question box, context-mode toggle, provider/model shown, generated SQL (expandable), results table, optional summary, per-question latency/tokens/cost, and data version currently loaded.
- Thin FastAPI layer exposing the same pipeline so other front ends can be built later.

### Phase 8 — Performance and scale-up readiness (later; plan only when I ask)
Run `medium`/`large` generation, benchmark locally and on ADLS, tune row groups / sort order / partitioning / caching, document findings in `docs/performance.md`, and prepare the path for replacing synthetic data with real data through the schema contract.

## 7. Deliverables (by end of Phase 7)

- Clean repo with README (setup, architecture, how to run each component), `CLAUDE.md`, `docs/PROGRESS.md`, phase plans.
- Architecture diagram (Mermaid) in `docs/architecture.md`.
- Evaluation report comparing the three context modes on the development model.
- Switching to a production model (e.g. Claude via Microsoft Foundry) requires only config changes, followed by re-running the evaluation.

## 8. Start

Read this brief fully. Then begin **Phase 0**: write `docs/plans/phase-0.md` and wait for my approval. In the same plan, list any questions or concerns about the overall brief.
