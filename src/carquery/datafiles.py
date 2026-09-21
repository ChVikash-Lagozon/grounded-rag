"""Where each table's parquet files live, and a DuckDB connection with one view per table.

Layout under ``data_root``: ``<table>/**/*.parquet``. Dimensions are one file, facts are a
history file plus one file per daily increment, or ``year=/month=`` partitions. Partition
values are derived from the table's date column, so views read files with
``hive_partitioning = false`` and expose exactly the contract columns.

:func:`connect_views` globs a local ``data_root`` (generator, profile, tests). The catalog uses
:func:`create_file_views`, which pins each view to an explicit file list from a connector.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


def read_files_sql(uris: Sequence[str]) -> str:
    """A ``read_parquet`` call over an explicit list of files."""
    listed = ", ".join("'" + uri.replace("'", "''") + "'" for uri in uris)
    return f"read_parquet([{listed}], hive_partitioning = false)"


def create_file_views(
    con: duckdb.DuckDBPyConnection,
    files: Mapping[str, Sequence[str]],
    columns: Mapping[str, Sequence[str]] | None = None,
) -> None:
    """Create (or replace) one view per table over its files; tables without files get none.

    ``columns`` (per table) limits a view to those columns, in that order; otherwise ``*``.
    """
    for name, uris in files.items():
        if not uris:
            con.execute(f"DROP VIEW IF EXISTS {name}")
            continue
        selected = columns.get(name) if columns else None
        select = ", ".join(f'"{c}"' for c in selected) if selected else "*"
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT {select} FROM {read_files_sql(uris)}")
