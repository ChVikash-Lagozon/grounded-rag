from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest

from carquery.validation import validate

from .conftest import Dataset


@pytest.fixture
def data_copy(dataset: Dataset, tmp_path: Path) -> Path:
    root = tmp_path / "data"
    shutil.copytree(dataset.root, root)
    return root


def _rewrite(root: Path, table: str, select: str) -> None:
    """Replace a table's files with the result of ``select`` (``t`` = current table)."""
    directory = root / table
    source = f"read_parquet('{directory.as_posix()}/**/*.parquet')"
    temp = root / f"{table}.tmp.parquet"
    duckdb.connect().execute(
        f"COPY (WITH t AS (SELECT * FROM {source}) {select}) TO '{temp.as_posix()}' (FORMAT parquet)"
    )
    shutil.rmtree(directory)
    directory.mkdir()
    temp.rename(directory / f"{table}.parquet")


def _only(check_prefix: str) -> Callable[[Path, Dataset], None]:
    def assert_issue(root: Path, dataset: Dataset) -> None:
        report = validate(root, dataset.contract)
        assert not report.ok
        assert any(i.check.startswith(check_prefix) for i in report.issues), report.render()

    return assert_issue


CORRUPTIONS = {
    "duplicate_primary_key": (
        "dim_plant",
        "SELECT * FROM t UNION ALL SELECT * FROM t WHERE plant_id = 1",
        "primary_key_unique",
    ),
    "null_in_required_column": (
        "dim_plant",
        "SELECT * REPLACE (CASE WHEN plant_id = 2 THEN NULL ELSE plant_name END AS plant_name) FROM t",
        "not_null:plant_name",
    ),
    "value_outside_allowed_values": (
        "dim_dealer",
        "SELECT * REPLACE (CASE WHEN dealer_id = 1 THEN 'Gold' ELSE dealer_tier END AS dealer_tier) FROM t",
        "allowed_values:dealer_tier",
    ),
    "value_below_min": (
        "fact_part_supply",
        "SELECT * REPLACE (CASE WHEN supply_batch_id % 50 = 0 THEN -1 ELSE quantity END AS quantity) FROM t",
        "min:quantity",
    ),
    "failed_row_check": (
        "fact_sales",
        "SELECT * REPLACE (CASE WHEN sale_id = 1 THEN (net_price + 1)::DECIMAL(12,2) ELSE net_price END AS net_price) FROM t",
        "check:net_price_consistent",
    ),
    "orphan_foreign_key": (
        "dim_customer",
        "SELECT * FROM t WHERE customer_id <> (SELECT min(customer_id) FROM t)",
        "foreign_key:customer_id",
    ),
    "wrong_column_type": (
        "dim_plant",
        "SELECT * REPLACE (daily_capacity::VARCHAR AS daily_capacity) FROM t",
        "schema",
    ),
    "missing_column": (
        "dim_supplier",
        "SELECT * EXCLUDE (quality_rating) FROM t",
        "schema",
    ),
}


@pytest.mark.parametrize("case", list(CORRUPTIONS))
def test_validation_catches_corruption(case: str, dataset: Dataset, data_copy: Path) -> None:
    table, select, check = CORRUPTIONS[case]
    _rewrite(data_copy, table, select)
    _only(check)(data_copy, dataset)


def test_missing_table_is_an_error(dataset: Dataset, data_copy: Path) -> None:
    shutil.rmtree(data_copy / "dim_part")
    report = validate(data_copy, dataset.contract)
    assert not report.ok
    checks = {(i.table, i.check) for i in report.issues}
    assert ("dim_part", "files") in checks
    assert ("fact_warranty_claim", "foreign_key:part_id") in checks


def test_report_renders_failures(dataset: Dataset, data_copy: Path) -> None:
    _rewrite(data_copy, "dim_plant", CORRUPTIONS["duplicate_primary_key"][1])
    text = validate(data_copy, dataset.contract).render()
    assert text.startswith("Validation FAILED")
    assert "dim_plant: primary_key_unique" in text
