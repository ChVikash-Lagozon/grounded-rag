"""Data quality validation of the parquet files against the schema contract.

Checks, per table: files exist, schema (column names and types), primary key unique and not
null, not-null columns, allowed values, min/max ranges, row-level ``checks`` expressions and
foreign-key integrity. Everything runs as DuckDB SQL over the parquet files, so it works the
same for local and (Phase 2) remote storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import duckdb

from carquery.contract import SchemaContract, Table
from carquery.datafiles import connect_views, table_files
from carquery.logging import get_logger

Severity = Literal["error", "warning"]
SAMPLE_SIZE = 3
log = get_logger(__name__)


@dataclass
class Issue:
    table: str
    check: str
    message: str
    failing_rows: int = 0
    sample: list[str] = field(default_factory=list)
    severity: Severity = "error"

    def render(self) -> str:
        text = f"[{self.severity}] {self.table}: {self.check}: {self.message}"
        if self.failing_rows:
            text += f" ({self.failing_rows:,} rows)"
        if self.sample:
            text += f" e.g. {', '.join(self.sample)}"
        return text


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    checks_run: int = 0

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def render(self) -> str:
        status = "PASSED" if self.ok else "FAILED"
        lines = [
            f"Validation {status}: {len(self.row_counts)} tables, {self.checks_run} checks, "
            f"{len(self.issues)} issue(s)"
        ]
        lines += [f"  {issue.render()}" for issue in self.issues]
        return "\n".join(lines)


def validate(data_root: Path, contract: SchemaContract) -> ValidationReport:
    """Validate every contract table under ``data_root``."""
    report = ValidationReport()
    con = connect_views(data_root, contract)
    try:
        present = []
        for name, table in contract.tables.items():
            report.checks_run += 1
            if not table_files(data_root, name):
                report.issues.append(Issue(name, "files", "no parquet files found"))
                continue
            if _check_schema(con, table, report):
                present.append(table)
        for table in present:
            report.row_counts[table.name] = _scalar(con, f"SELECT count(*) FROM {table.name}")
            _check_table(con, table, report)
        present_names = {t.name for t in present}
        for table in present:
            _check_foreign_keys(con, table, present_names, report)
    finally:
        con.close()
    log.info(
        "validation_finished", ok=report.ok, issues=len(report.issues), checks=report.checks_run
    )
    return report


def _check_schema(con: duckdb.DuckDBPyConnection, table: Table, report: ValidationReport) -> bool:
    actual = {row[0]: row[1] for row in con.execute(f"DESCRIBE {table.name}").fetchall()}
    expected = {c.name: c.type for c in table.columns}
    ok = True
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        report.issues.append(Issue(table.name, "schema", f"missing columns: {missing}"))
        ok = False
    if extra:
        report.issues.append(Issue(table.name, "schema", f"unexpected columns: {extra}"))
        ok = False
    for name in sorted(set(expected) & set(actual)):
        if actual[name] != expected[name]:
            report.issues.append(
                Issue(
                    table.name,
                    "schema",
                    f"column {name} has type {actual[name]}, expected {expected[name]}",
                )
            )
            ok = False
    return ok


def _check_table(con: duckdb.DuckDBPyConnection, table: Table, report: ValidationReport) -> None:
    name = table.name
    pk = ", ".join(table.primary_key)

    _count_check(
        con,
        report,
        name,
        "primary_key_unique",
        f"SELECT {pk} FROM {name} GROUP BY {pk} HAVING count(*) > 1",
        f"duplicate primary key ({pk})",
    )
    for column in table.columns:
        c = column.name
        if not column.nullable or c in table.primary_key:
            _count_check(
                con,
                report,
                name,
                f"not_null:{c}",
                f"SELECT {pk} FROM {name} WHERE {c} IS NULL",
                f"{c} is NULL",
            )
        if column.allowed_values:
            values = ", ".join("'" + v.replace("'", "''") + "'" for v in column.allowed_values)
            _count_check(
                con,
                report,
                name,
                f"allowed_values:{c}",
                f"SELECT {c} FROM {name} WHERE {c} NOT IN ({values})",
                f"{c} has values outside {column.allowed_values}",
            )
        if column.min is not None:
            _count_check(
                con,
                report,
                name,
                f"min:{c}",
                f"SELECT {c} FROM {name} WHERE {c} < {column.min}",
                f"{c} below {column.min:g}",
            )
        if column.max is not None:
            _count_check(
                con,
                report,
                name,
                f"max:{c}",
                f"SELECT {c} FROM {name} WHERE {c} > {column.max}",
                f"{c} above {column.max:g}",
            )
    for check in table.checks:
        _count_check(
            con,
            report,
            name,
            f"check:{check.name}",
            f"SELECT {pk} FROM {name} WHERE NOT ({check.expr})",
            f"violates {check.expr}",
        )


def _check_foreign_keys(
    con: duckdb.DuckDBPyConnection, table: Table, present: set[str], report: ValidationReport
) -> None:
    for fk in table.foreign_keys:
        check = f"foreign_key:{','.join(fk.columns)}"
        if fk.ref_table not in present:
            report.checks_run += 1
            report.issues.append(
                Issue(table.name, check, f"referenced table {fk.ref_table} is missing or invalid")
            )
            continue
        join = " AND ".join(
            f"r.{rc} = t.{c}" for c, rc in zip(fk.columns, fk.ref_columns, strict=True)
        )
        not_null = " AND ".join(f"t.{c} IS NOT NULL" for c in fk.columns)
        cols = ", ".join(f"t.{c}" for c in fk.columns)
        _count_check(
            con,
            report,
            table.name,
            check,
            f"SELECT DISTINCT {cols} FROM {table.name} t WHERE {not_null} AND NOT EXISTS "
            f"(SELECT 1 FROM {fk.ref_table} r WHERE {join})",
            f"values not found in {fk.references}",
        )


def _count_check(
    con: duckdb.DuckDBPyConnection,
    report: ValidationReport,
    table: str,
    check: str,
    failing_query: str,
    message: str,
) -> None:
    """Run a query returning failing rows; record an issue if there are any."""
    report.checks_run += 1
    try:
        count = _scalar(con, f"SELECT count(*) FROM ({failing_query})")
        if not count:
            return
        rows = con.execute(f"{failing_query} LIMIT {SAMPLE_SIZE}").fetchall()
        sample = [str(row[0]) if len(row) == 1 else str(tuple(row)) for row in rows]
        report.issues.append(Issue(table, check, message, count, sample))
    except duckdb.Error as exc:
        report.issues.append(Issue(table, check, f"check could not run: {exc}"))


def _scalar(con: duckdb.DuckDBPyConnection, query: str) -> int:
    row = con.execute(query).fetchone()
    return int(row[0]) if row else 0
