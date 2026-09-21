"""Where each table's parquet files live, and a DuckDB connection with one view per table.

Layout under ``data_root``: ``<table>/**/*.parquet``. Dimensions are one file, facts are a
history file plus one file per daily increment, or ``year=/month=`` partitions. Partition
values are derived from the table's date column, so views read files with
``hive_partitioning = false`` and expose exactly the contract columns. Phase 2 builds the
persistent catalog on top of this.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from carquery.contract import SchemaContract


def sql_path(path: Path) -> str:
    """A path as a DuckDB string literal body (forward slashes, quotes escaped)."""
    return path.as_posix().replace("'", "''")


def table_glob(data_root: Path, table: str) -> str:
    return f"{sql_path(data_root / table)}/**/*.parquet"


def table_files(data_root: Path, table: str) -> list[Path]:
    directory = data_root / table
    return sorted(directory.rglob("*.parquet")) if directory.is_dir() else []


def connect_views(
    data_root: Path,
    contract: SchemaContract,
    con: duckdb.DuckDBPyConnection | None = None,
) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB connection with a view per contract table that has files."""
    con = con or duckdb.connect()
    for name in contract.tables:
        if table_files(data_root, name):
            con.execute(
                f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM "
                f"read_parquet('{table_glob(data_root, name)}', hive_partitioning = false)"
            )
    return con
