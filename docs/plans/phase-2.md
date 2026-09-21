# Phase 2 Plan — Storage Connectors, Catalog, Refresh

Status: **approved 2026-09-21, local-only scope** (see §0)

## 0. Decisions at approval (override the rest of this plan)

- **No ADLS account for dev yet, so Phase 2 is local-only.** The `StorageConnector` interface,
  the `storage.backend` switch and the connector factory are built; only `LocalConnector` is
  implemented. `ADLSConnector`, its config section and env vars, `carq storage upload` and the
  Local ↔ ADLS parity test are **deferred** until an account exists (open questions 4 and 5).
  Adding it later means one new connector class and one registry entry.
- Open questions 1, 2, 3 and 6 are approved with their defaults: views pinned to the scanned file
  list, failed validation rejects the refresh (`--allow-invalid` overrides), `datasets.yaml` holds
  only paths/patterns, and partition pruning is deferred to Phase 8.
- **No separate lock file.** DuckDB already allows only one read-write process per catalog
  file, and the lock is released if a process crashes. Refresh opens the catalog read-write only
  for short steps (reading state, activating) and retries briefly if the file is busy. Activation
  re-checks the fingerprint inside its transaction, so two overlapping refreshes can't create
  duplicate versions.

## 1. Goal and scope

Make the parquet data queryable through a persistent DuckDB **catalog** whose contents change
only when someone runs **refresh**, and make the storage location (local disk or ADLS) a
config-only choice.

In scope:

- a `StorageConnector` interface with `LocalConnector` and `ADLSConnector`;
- `config/datasets.yaml` (static definitions only);
- the catalog `.duckdb` file: one view per table plus runtime state (data version, refresh log);
- `carq refresh`: scan → validate → rebuild views → record basic stats → new data version;
- the Local ↔ ADLS switch, with a test that runs identical queries against both.

Out of scope, following phase-0 §3:

- a loader, version folders, rollback and retention (loading is external);
- the query engine's read-only rules, limits and timeouts (Phase 3);
- rich context statistics (Phase 4). Phase 2 records only row counts and date ranges, and
  leaves a hook for Phase 4.
- result cache invalidation (Phase 5). The cache will be keyed by data version, so a new
  version is all it needs.
- the Refresh button and API endpoint (Phase 7). Both will call the same `refresh()` function
  that the CLI uses.

## 2. Dependencies

**No new required dependencies.** DuckDB's own `azure` extension reads ADLS (`abfss://` /
`az://`) and supports every auth method we need through DuckDB secrets:

| Auth method | DuckDB secret |
|---|---|
| Connection string | `TYPE azure, PROVIDER config, CONNECTION_STRING …` |
| Service principal | `TYPE azure, PROVIDER service_principal, TENANT_ID, CLIENT_ID, CLIENT_SECRET, ACCOUNT_NAME` |
| Managed identity / Azure CLI / env | `TYPE azure, PROVIDER credential_chain, CHAIN 'managed_identity;cli;env', ACCOUNT_NAME` |

DuckDB can also list remote files with sizes and modification times (`read_blob` without the
content column), which covers the scan step.

- **The azure extension is downloaded on first use.** DuckDB fetches it from
  `extensions.duckdb.org`. For offline or locked-down machines there's an optional
  `duckdb.extension_directory` setting, plus `carq storage check` to pre-install it.
- **Upload helper (open question 5).** Copying the local dataset to ADLS needs either DuckDB
  writing to Azure (I'll check whether the current extension version supports writes) or an
  optional `azure` extra (`azure-storage-blob`, `azure-identity`). The extra would be
  optional, only needed for the upload command, and never used by the core read path.

## 3. Configuration

### 3.1 Storage: a new `storage:` section in `app.yaml`

It goes in `app.yaml` so that `CARQ__STORAGE__BACKEND=adls` works as a one-line switch.

```yaml
storage:
  backend: ${STORAGE_BACKEND:-local}        # local | adls
  local:
    root: ${DATA_ROOT:-data}                # replaces paths.data_root (kept as an alias)
  adls:
    account_name: ${AZURE_STORAGE_ACCOUNT:-}
    container: ${AZURE_STORAGE_CONTAINER:-}
    prefix: ${AZURE_STORAGE_PREFIX:-carquery}   # folder inside the container
    auth: ${AZURE_STORAGE_AUTH:-credential_chain}  # connection_string | service_principal | credential_chain
    credential_chain: "cli;managed_identity;env"
    connection_string: ${AZURE_STORAGE_CONNECTION_STRING:-}   # SecretStr
    tenant_id: ${AZURE_TENANT_ID:-}
    client_id: ${AZURE_CLIENT_ID:-}
    client_secret: ${AZURE_CLIENT_SECRET:-}                   # SecretStr
duckdb:
  extension_directory: null                 # optional, for offline extension installs
```

- New env vars go into `.env.example`: `STORAGE_BACKEND`, `AZURE_STORAGE_ACCOUNT`,
  `AZURE_STORAGE_CONTAINER`, `AZURE_STORAGE_PREFIX`, `AZURE_STORAGE_AUTH`.
- Validation checks that the chosen auth method has its required fields. Secrets are
  `SecretStr` and are redacted in `carq config show`.
- **The generator always writes locally** (to `storage.local.root`). With `backend: adls`,
  data reaches ADLS through the external load process, or through the upload helper for
  dev/testing.

### 3.2 `config/datasets.yaml` holds static definitions only

```yaml
defaults:
  path: "{table}"              # folder under the storage root
  pattern: "**/*.parquet"
tables: {}                     # per-table overrides, e.g.
#  fact_sales: {path: sales/fact_sales}
```

Every table in the schema contract is a dataset. **Descriptions stay in the contract**, not in
a second copy (open question 3). A dataset listed here that isn't in the contract, or the other
way round, is a config error.

## 4. Storage connectors — `carquery.storage`

```python
@dataclass(frozen=True)
class FileInfo:
    uri: str  # what DuckDB reads: absolute local path or abfss://…
    size: int
    modified: datetime


class StorageConnector(Protocol):
    name: str  # "local" | "adls"

    def configure(
        self, con: duckdb.DuckDBPyConnection
    ) -> None: ...  # load extension, create TEMPORARY secret
    def dataset_uri(self, dataset: Dataset) -> str: ...  # glob URI for a dataset
    def list_files(self, dataset: Dataset) -> list[FileInfo]: ...
    def describe(self) -> str: ...  # for logs/UI, no secrets
```

- **`LocalConnector`** lists files with `pathlib` and returns absolute paths.
- **`ADLSConnector`**: `configure()` runs `LOAD azure` and creates a **temporary** (per
  connection, in memory) secret, so credentials are never written to disk. `list_files()` uses
  `read_blob(uri)` for filename, size and last_modified. URIs look like
  `abfss://<container>@<account>.dfs.core.windows.net/<prefix>/<path>/`.
- `get_connector(config)` is a factory keyed by `backend`. Adding S3, GCS or MinIO later means
  one new class plus one registry entry; core code doesn't change.
- Secrets never appear in logs, errors or the catalog.

## 5. Catalog — `carquery.catalog`

It is a persistent DuckDB file at `paths.catalog_path`, default `data/catalog.duckdb`, and it
contains:

| Object | Contents |
|---|---|
| one **view per table** | `SELECT <contract columns> FROM read_parquet([<file list>], hive_partitioning = false)` |
| `_refresh_log` | one row per refresh attempt: id, started/finished, status (`active`, `rejected`, `no_change`, `error`), fingerprint, file count, error summary, validation issue count |
| `_data_version` | the active version: version id, fingerprint, created_at, data-through date (max fact date), validated flag, connector description |
| `_table_stats` | per table and version: row count, file count, bytes, min/max of the date column. Phase 4 adds columns or tables here |
| `_files` | the active file list per table (uri, size, modified). Used for fingerprinting and diffing |

### Decision A: views are pinned to the file list captured at refresh (open question 1)

Phase 0 described glob-based views. I recommend **pinning**: each view lists the exact files
seen by the last successful refresh.

- **Why:** new files that land between refreshes stay invisible until someone refreshes. The
  data version then really describes what queries see, cached results stay correct, and a
  failed validation can keep the previous facts out of view (see decision B).
- **Cost:** view definitions get long when there are thousands of files. That's fine for
  DuckDB; Phase 8 can compact.
- Dimensions are overwritten in place at the same path, so pinning can't freeze them. That is
  inherent to the "dimensions are full reloads" decision.

### Decision B: what a failed validation does (the question left open in phase-0 §3)

| Option | Behaviour |
|---|---|
| **B1 (recommended)** | The refresh is **rejected**. The views keep the previous pinned file list, so new fact files stay invisible, and the log records `rejected` with the issues. `carq refresh --allow-invalid` activates anyway and marks the version `validated = false` so the UI can warn. Caveat: dimension files already overwritten on disk are what the views read, because of the in-place reload. |
| B2 | Snapshot everything into DuckDB tables on each refresh, so a rejected refresh changes nothing. It duplicates all data into the catalog, gets heavy at medium/large, and makes ADLS pointless for querying. Not recommended. |

### The query side (Phase 3 uses this)

- `open_catalog(config, read_only=True)` opens the file **read-only**, then calls
  `connector.configure(con)`, because Azure secrets are per connection and never persisted.
- **Concurrency:** DuckDB allows either many read-only processes or one writer. Readers use
  short-lived connections (per request). `refresh` takes a lock file (`catalog.duckdb.lock`)
  so two refreshes can't overlap, and retries briefly if a reader holds the file. This is
  noted as a known limitation for the Streamlit phase.

## 6. Refresh — `carquery.refresh.refresh(config) -> RefreshResult`

1. **Scan:** `list_files()` for every dataset. A missing required table fails the refresh.
2. **Fingerprint:** SHA-256 over the sorted (uri, size, modified) list. If it equals the active
   version's fingerprint, stop with `no_change` unless `--force`.
3. **Validate:** create temporary views over the scanned files on a working connection and run
   `carquery.validation` there. Validation is refactored to take a connection with views
   instead of a local `data_root`, so local and ADLS share one code path.
4. **Decide:** on errors, record `rejected` and stop (decision B1), unless `--allow-invalid`.
5. **Activate** (one catalog transaction): recreate the views with the new pinned file lists,
   write `_files`, `_table_stats` and a new `_data_version` row (`version_id` increments;
   label like `v3 · 2026-09-21 12:04 · a1b2c3d4`; data-through = max fact date), and append to
   `_refresh_log`.
6. **Post-refresh hooks:** a list of callables that run after activation. Phase 4 registers its
   statistics builder and Phase 5 its cache invalidation. Empty for now.

Every step logs structured events (`refresh_started`, `refresh_scanned`, `refresh_rejected`,
`refresh_activated`) with `bound_context(refresh_id=…)`.

## 7. CLI

```bash
carq refresh [--force] [--allow-invalid]   # prints version, per-table files/rows, issues
carq catalog status                          # active version, data-through date, validated?, last 5 refreshes
carq catalog sql "SELECT ..."                # dev convenience: run a query on the catalog (read-only; Phase 3 hardens it)
carq storage check                           # connector, auth method (no secrets), file counts per dataset
carq storage upload                          # dev helper: copy the local dataset to ADLS (open question 5)
carq validate                                # now validates whatever the configured connector points at
```

## 8. Files

```
config/app.yaml (storage + duckdb sections), config/datasets.yaml
src/carquery/storage/__init__.py, base.py, local.py, adls.py
src/carquery/datasets.py      # datasets.yaml model, contract cross-check
src/carquery/catalog.py       # open_catalog, schema of state tables, view building
src/carquery/refresh.py       # refresh(), fingerprint, hooks
src/carquery/validation.py    # refactor: validate(con, contract)
src/carquery/datafiles.py     # becomes a thin local helper over the connector (kept for tests)
tests/test_storage.py test_catalog.py test_refresh.py test_adls.py
```

## 9. Tests

- **Local connector:** lists files with size/mtime, builds URIs, handles the partitioned layout.
- **ADLS connector (unit, no network):** builds the correct `abfss://` URIs and the secret SQL
  for each auth method; missing required fields give clear errors; secrets never show up in
  `describe()` or logs.
- **Refresh lifecycle** on a copy of the `test` dataset:
  - the first refresh creates v1, and views return contract columns and the expected row
    counts;
  - a second refresh without changes is `no_change`;
  - `generate day 1` then refresh gives v2, with more fact rows and a later data-through
    date;
  - a corrupted dimension or orphan fact file makes the refresh `rejected`, v2 stays active,
    and the new fact files stay invisible (pinning);
  - `--allow-invalid` activates with `validated = false`.
- **Catalog:** a new read-only connection sees the views and state; a concurrent second
  refresh is blocked by the lock.
- **Local ↔ ADLS parity:** a set of queries (row counts, a join, an aggregate per pattern) run
  against the local catalog and the ADLS catalog must return identical results. Marked
  `adls` and **skipped unless** `CARQ_TEST_ADLS=1` and the ADLS settings are present. Before
  comparing, it uploads the `test` dataset to `<prefix>/test-<uuid>/` and removes it
  afterwards.

## 10. Open questions (defaults in bold)

1. **Views pinned to the file list captured at refresh** (decision A) instead of glob views
   that pick up new files immediately. This changes the phase-0 note about glob-based views.
2. **A failed validation rejects the refresh and keeps the previous version active**, with an
   `--allow-invalid` override (decision B1), rather than snapshotting data into DuckDB (B2).
3. **`datasets.yaml` holds only paths and patterns.** Descriptions stay in the schema contract.
   The brief's "description" field in the manifest would duplicate them.
4. **ADLS access for development.** Do you have a storage account (ADLS Gen2, hierarchical
   namespace on) and a container I can use for the parity test? Which auth for dev:
   **Azure CLI login** (`az login`, used through `credential_chain`), a connection string, or
   a service principal? If none is available yet, everything is built and unit-tested, and the
   parity test stays skipped until you have one. A local option is the Azurite emulator, but
   it only emulates Blob, not ADLS Gen2, and needs Node or Docker.
5. **Upload helper.** Options: **(a) `carq storage upload` via DuckDB if the azure extension can
   write, otherwise an optional `azure` extra** (`azure-storage-blob`, `azure-identity`), or
   (b) no helper, and you copy data with `azcopy` or Storage Explorer (then the parity test
   expects the data to already be there).
6. **Partition pruning on ADLS for medium/large:** views read with `hive_partitioning = false`
   and explicit file lists, so date filters don't prune files. **Deferred to Phase 8**, which
   can add partition-aware views.

## 11. Definition of done

`carq refresh` builds the catalog from local data and records v1. `generate day N` followed by
`refresh` creates a new version. A corrupted load is rejected and the previous version stays
active. `carq catalog status` and `carq storage check` work. ADLS is unit-tested (and the
parity test passes if you provide an account). `uv run pytest` is green and ruff is clean.
README, CLAUDE.md and PROGRESS.md are updated. Then one commit.
