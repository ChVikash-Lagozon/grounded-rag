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
