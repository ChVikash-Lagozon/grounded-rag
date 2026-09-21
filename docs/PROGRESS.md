# Progress

| Phase | Status |
|---|---|
| 0 — Scaffolding | ✅ done (2026-09-21) |
| 1 — Schema contract, synthetic generator, validation | next: plan to be written |
| 2 — Storage connectors, catalog, refresh | — |
| 3 — Query execution layer | — |
| 4 — Context builders and ontology | — |
| 5 — LLM provider layer and orchestration | — |
| 6 — Evaluation harness | — |
| 7 — Interface (Streamlit + FastAPI) | — |
| 8 — Performance / scale-up | later, plan on request |

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
