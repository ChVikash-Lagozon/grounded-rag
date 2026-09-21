# Architecture

This is the target architecture; components get built phase by phase (see `PROGRESS.md`). It
reflects the decisions in `plans/phase-0.md` §3: loading is external and we only build refresh,
and the LLM layer is provider-agnostic.

```mermaid
flowchart TD
    U[User] --> UI[Streamlit UI]
    U --> API[FastAPI]
    UI --> ORCH
    API --> ORCH
    UI -. Refresh data .-> REF

    subgraph Pipeline
        ORCH[Orchestrator] -->|1 table selection<br/>2 SQL generation| LLM
        ORCH --> CTX[Context Builder<br/>schema_only / +descriptions / +ontology]
        ORCH -->|3 EXPLAIN, retry ≤2<br/>4 execute| QE[Query Engine<br/>read-only, limits, timeouts]
        ORCH --> FS[Few-shot Retriever<br/>BM25 now, embeddings later]
        ORCH --> RC[(Result cache)]
    end

    subgraph LLM[LLM Provider interface]
        OAI[OpenAICompatibleProvider<br/>OpenRouter dev · Azure AI Foundry prod]
        MOCK[MockProvider]
        NATIVE[native adapters: later]
    end

    QE --> CAT[(DuckDB catalog<br/>views + data version)]
    CTX --> CAT
    REF[Refresh: scan → validate → rebuild views<br/>→ recompute stats → new data version] --> CAT
    REF -. invalidates .-> RC
    CAT --> SC[Storage Connector<br/>Local · ADLS · later S3/GCS/MinIO]
    SC --> PQ[(Parquet at fixed paths<br/>dims: full reload<br/>facts: append-only, year=/month=)]

    EXT[External load process<br/>or synthetic generator] --> PQ
    CONTRACT[[Schema contract]] -.-> EXT
    CONTRACT -.-> REF
    CONTRACT -.-> CTX
```

## Component status

Keep this table current at the end of every phase.

| Component | Module | Phase | Status |
|---|---|---|---|
| Config (YAML + env + `local.yaml`), logging | `carquery.config`, `carquery.logging` | 0 | ✅ built |
| Schema contract, ER diagram | `config/schema_contract.yaml`, `carquery.contract` | 1 | ✅ built |
| Synthetic generator (history + day N, 8 planted patterns) | `carquery.generator` | 1 | ✅ built |
| Validation (schema, keys, ranges, FKs) | `carquery.validation` | 1 | ✅ built |
| Dataset definitions | `config/datasets.yaml`, `carquery.datasets` | 2 | ✅ built |
| Storage connector | `carquery.storage` (`LocalConnector`) | 2 | ✅ local · ⏸ ADLS deferred |
| Catalog (pinned views + state tables) | `carquery.catalog` | 2 | ✅ built |
| Refresh (scan → validate → activate → hooks) | `carquery.refresh` | 2 | ✅ built |
| Query engine (sandbox, limits, EXPLAIN, errors) | `carquery.query` | 3 | ✅ built |
| Context builder + ontology | — | 4 | planned |
| LLM providers, orchestrator, few-shot, result cache | — | 5 | planned |
| Evaluation harness | — | 6 | planned |
| Streamlit UI + FastAPI | — | 7 | planned |

## Query path as built (Phases 2–3)

```mermaid
sequenceDiagram
    participant C as Caller (CLI now, orchestrator later)
    participant E as QueryEngine
    participant S as Shared sandbox (per process)
    participant K as Catalog .duckdb
    C->>E: execute(sql, max_rows, timeout)
    E->>E: statement checks (1 SELECT, no PRAGMA/CALL, size)
    E->>S: acquire (open if idle)
    S->>K: read-only open, read active version + pinned files
    S->>S: allowed_paths = pinned files, no external access, lock config
    E->>S: EXPLAIN wrapped SQL (worker thread)
    E->>S: SELECT * FROM (sql) LIMIT n+1 (worker thread, interrupt on timeout)
    S-->>E: rows / DuckDB error
    E->>S: release (close when last query ends)
    E-->>C: QueryResult or QueryError(kind, message, hint, retryable)
```

Refresh (`carq refresh`) is the only writer. It waits while queries in the same process hold
the sandbox (the "different configuration" error is treated as busy), and it activates a new
version in one transaction. See `decisions.md` (D-20 … D-31) for why.
