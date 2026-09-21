"""The sandbox: every capability beyond reading the catalog views must be refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from carquery.config import AppConfig
from carquery.query import QueryEngine, QueryError


def _snapshot(root: Path) -> dict[str, int]:
    return {
        p.relative_to(root).as_posix(): p.stat().st_size for p in root.rglob("*") if p.is_file()
    }


def _cases(data_root: Path, outside: Path) -> list[tuple[str, set[str]]]:
    truth = (data_root / "ground_truth.yaml").as_posix()
    other = outside.as_posix()
    sales = next((data_root / "fact_sales").rglob("*.parquet")).as_posix()
    catalog = (data_root / "catalog.duckdb").as_posix()
    rejected = {"not_read_only"}
    forbidden = {"forbidden"}
    return [
        # writes and DDL
        ("INSERT INTO _data_version SELECT * FROM _data_version", rejected),
        ("UPDATE _data_version SET validated = false", rejected),
        ("DELETE FROM _files", rejected),
        ("DROP VIEW fact_sales", rejected),
        ("CREATE TABLE x AS SELECT 1", rejected),
        ("CREATE TEMP MACRO f(a) AS a + 1", rejected),
        (f"COPY fact_sales TO '{data_root.as_posix()}/leak.csv'", rejected),
        (f"EXPORT DATABASE '{data_root.as_posix()}/export'", rejected),
        # engine control
        (f"ATTACH '{catalog}' AS again", rejected),
        ("INSTALL httpfs", rejected),
        ("LOAD httpfs", rejected),
        ("SET enable_external_access = true", rejected),
        ("RESET lock_configuration", rejected),
        ("PRAGMA database_list", rejected),
        ("CALL pragma_version()", rejected),
        ("SELECT 1; DROP VIEW fact_sales", {"multiple_statements"}),
        # file access through SELECT
        (f"SELECT * FROM read_text('{truth}')", forbidden),
        (f"SELECT * FROM read_csv('{truth}')", forbidden),
        (f"SELECT * FROM '{truth}'", {"forbidden", "unknown_table"}),
        (f"SELECT * FROM read_text('{other}')", forbidden),
        (f"SELECT * FROM glob('{data_root.as_posix()}/*')", forbidden),
        # (DuckDB always lets a connection read its own database file and its .wal, so
        # read_blob on the catalog is allowed; it holds nothing the views don't expose.)
        (f"SELECT * FROM read_parquet('{sales[:-8]}*.parquet')", forbidden),
        ("SELECT * FROM duckdb_secrets()", forbidden),
        ("SELECT * FROM read_json('https://example.com/x.json')", forbidden),
    ]


@pytest.fixture(scope="module")
def outside_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("outside") / "secret.txt"
    path.write_text("do not read", encoding="utf-8")
    return path


def test_every_blocked_capability_is_refused(
    refreshed_catalog: AppConfig, outside_file: Path
) -> None:
    engine = QueryEngine(refreshed_catalog)
    data_root = refreshed_catalog.paths.data_root
    before = _snapshot(data_root)
    failures = []
    for sql, kinds in _cases(data_root, outside_file):
        try:
            engine.execute(sql)
            failures.append(f"ALLOWED: {sql}")
        except QueryError as error:
            if error.kind not in kinds:
                failures.append(f"{sql}: expected {kinds}, got {error.kind}: {error.message}")
    assert not failures, "\n".join(failures)
    after = _snapshot(data_root)
    assert {k: v for k, v in after.items() if not k.startswith("catalog.duckdb")} == {
        k: v for k, v in before.items() if not k.startswith("catalog.duckdb")
    }


def test_pinned_views_still_read(refreshed_catalog: AppConfig) -> None:
    engine = QueryEngine(refreshed_catalog)
    result = engine.execute(
        "SELECT count(*) FROM fact_warranty_claim w JOIN dim_part p USING (part_id)"
    )
    assert result.rows[0][0] > 0
