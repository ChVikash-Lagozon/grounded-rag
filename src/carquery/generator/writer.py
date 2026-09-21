"""Staging and final parquet writing.

The simulation writes each chunk to ``<data_root>/.staging/<table>/`` as plain parquet with
internal helper columns (``_arrival``, ``_available``, ``_decision_date``). The final files are
produced by DuckDB ``COPY``: it casts every column to its contract type, keeps the rows visible
at the snapshot date, sorts by the table's date column, and writes ZSTD parquet with the
preset's row group size (optionally ``year=/month=`` partitioned). DuckDB sorts out of core, so
this also works for the medium/large presets.
"""

from __future__ import annotations

import os
import shutil
from datetime import date
from pathlib import Path
from typing import Literal

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from carquery.contract import SchemaContract, Table
from carquery.datafiles import sql_path

STAGING_DIR = ".staging"
Mode = Literal["history", "day"]


class Staging:
    def __init__(self, data_root: Path) -> None:
        self.root = data_root / STAGING_DIR
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self._count: dict[str, int] = {}

    def add(self, table: str, data: pa.Table) -> None:
        n = self._count.get(table, 0)
        directory = self.root / table
        directory.mkdir(exist_ok=True)
        pq.write_table(data, directory / f"part-{n:05d}.parquet", compression="none")
        self._count[table] = n + 1

    def tables(self) -> list[str]:
        return sorted(self._count)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def write_table(
    con: duckdb.DuckDBPyConnection,
    staging: Staging,
    table: Table,
    data_root: Path,
    as_of: date,
    mode: Mode,
    row_group_size: int,
    partitioned: bool,
) -> list[Path]:
    """Write one contract table from staging; returns the files written."""
    source = (
        f"read_parquet('{sql_path(staging.root / table.name)}/*.parquet', union_by_name = true)"
    )
    staged = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}
    as_of_sql = f"DATE '{as_of.isoformat()}'"

    select = ", ".join(
        f"CAST({_column_expr(table.name, c.name, as_of_sql)} AS {c.type}) AS {c.name}"
        for c in table.columns
    )
    where = []
    if "_available" in staged:
        where.append(f"_available <= {as_of_sql}")
    if "_arrival" in staged:
        where.append(f"_arrival {'<=' if mode == 'history' else '='} {as_of_sql}")
    order = [f"{table.date_column} NULLS LAST"] if table.date_column else []
    order += table.primary_key
    query = f"SELECT {select} FROM {source}"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY " + ", ".join(order)

    out = staging.root / "_out" / table.name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    options = f"FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {row_group_size}"
    label = "history" if mode == "history" else as_of.isoformat()

    if partitioned:
        if not table.date_column:
            raise ValueError(f"{table.name} has no date_column to partition by")
        query = (
            f"SELECT *, year({table.date_column}) AS year, "
            f"lpad(month({table.date_column})::VARCHAR, 2, '0') AS month FROM ({query})"
        )
        prefix = "history" if mode == "history" else f"day_{label}"
        con.execute(
            f"COPY ({query}) TO '{sql_path(out)}' "
            f"({options}, PARTITION_BY (year, month), FILENAME_PATTERN '{prefix}_{{i}}')"
        )
    else:
        name = table.name if table.kind == "dimension" else f"{table.name}_{label}"
        con.execute(f"COPY ({query}) TO '{sql_path(out / name)}.parquet' ({options})")

    target = data_root / table.name
    if table.kind == "dimension" and target.exists():
        shutil.rmtree(target)  # dimensions are full reloads
    written = []
    for file in sorted(out.rglob("*.parquet")):
        destination = target / file.relative_to(out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(file, destination)
        written.append(destination)
    return written


def _column_expr(table: str, column: str, as_of_sql: str) -> str:
    """Value of a column as seen on the snapshot date (row state known at that date)."""
    if table == "dim_dealer" and column == "closed_date":
        return f"CASE WHEN closed_date <= {as_of_sql} THEN closed_date END"
    if table == "dim_dealer" and column == "is_active":
        return f"NOT coalesce(closed_date <= {as_of_sql}, false)"
    if table == "fact_warranty_claim" and column == "claim_status":
        return f"CASE WHEN _decision_date > {as_of_sql} THEN 'Pending' ELSE claim_status END"
    if table == "fact_recall_vehicle" and column == "remedy_completed_date":
        return f"CASE WHEN remedy_completed_date <= {as_of_sql} THEN remedy_completed_date END"
    return column


def write_all(
    staging: Staging,
    contract: SchemaContract,
    data_root: Path,
    as_of: date,
    mode: Mode,
    row_group_size: int,
    partitioned_tables: list[str],
) -> dict[str, list[Path]]:
    con = duckdb.connect()
    try:
        return {
            name: write_table(
                con,
                staging,
                table,
                data_root,
                as_of,
                mode,
                row_group_size,
                partitioned=name in partitioned_tables,
            )
            for name, table in contract.tables.items()
            if mode == "history" or table.kind == "dimension" or name in staging.tables()
        }
    finally:
        con.close()
