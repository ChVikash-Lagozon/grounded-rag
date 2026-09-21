from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from carquery.catalog import active_files, active_version, open_catalog, refresh_history
from carquery.generator import generate_day
from carquery.refresh import RefreshResult, fingerprint, refresh
from carquery.storage import FileInfo

from .conftest import CatalogSetup


def _refresh(
    setup: CatalogSetup, force: bool = False, allow_invalid: bool = False
) -> RefreshResult:
    return refresh(
        setup.config,
        setup.data.contract,
        setup.datasets,
        force=force,
        allow_invalid=allow_invalid,
        hooks=[],
    )


def _count(setup: CatalogSetup, table: str) -> int:
    con = open_catalog(setup.config)
    try:
        row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    finally:
        con.close()
    assert row is not None
    return int(row[0])


def _add_duplicate_sales_file(root: Path) -> Path:
    """A new fact_sales file that repeats existing rows: a primary-key violation."""
    directory = root / "fact_sales"
    target = directory / "zz_duplicate.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_parquet('{directory.as_posix()}/**/*.parquet') LIMIT 5) "
        f"TO '{target.as_posix()}' (FORMAT parquet)"
    )
    return target


def test_first_refresh_creates_v1(catalog_setup: CatalogSetup) -> None:
    result = _refresh(catalog_setup)

    assert result.status == "active", result.message
    assert result.previous is None
    assert result.version is not None and result.version.version_id == 1
    assert result.version.validated
    assert result.version.label.startswith("v1 | ")
    assert result.report is not None and result.report.ok

    contract = catalog_setup.data.contract
    by_table = {s.table: s for s in result.stats}
    assert set(by_table) == set(contract.tables)
    fact_dates = [by_table[t.name].max_date for t in contract.facts if t.date_column]
    assert result.version.data_through == max(d for d in fact_dates if d)

    con = open_catalog(catalog_setup.config)
    try:
        assert active_version(con) == result.version
        for name, table in contract.tables.items():
            columns = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()]
            assert columns == table.column_names
            count = con.execute(f"SELECT count(*) FROM {name}").fetchone()
            assert count is not None and count[0] == by_table[name].row_count
        assert active_files(con) == result.files
    finally:
        con.close()


def test_unchanged_files_give_no_change_and_force_makes_a_version(
    catalog_setup: CatalogSetup,
) -> None:
    first = _refresh(catalog_setup)
    again = _refresh(catalog_setup)
    assert again.status == "no_change"
    assert again.version == first.version
    assert again.report is None  # nothing was validated

    forced = _refresh(catalog_setup, force=True)
    assert forced.status == "active"
    assert forced.version is not None and forced.version.version_id == 2
    assert forced.version.fingerprint == first.fingerprint

    con = open_catalog(catalog_setup.config)
    try:
        statuses = [e.status for e in refresh_history(con, limit=10)]
    finally:
        con.close()
    assert statuses == ["active", "no_change", "active"]


def test_new_day_of_data_creates_v2(catalog_setup: CatalogSetup) -> None:
    first = _refresh(catalog_setup)
    sales_before = _count(catalog_setup, "fact_sales")
    data = catalog_setup.data
    generate_day(data.generator, data.reference, data.contract, data.root, 1)

    second = _refresh(catalog_setup)

    assert second.status == "active", second.message
    assert second.version is not None and first.version is not None
    assert second.version.version_id == 2
    assert second.version.data_through == first.version.data_through + timedelta(days=1)
    assert len(second.files["fact_sales"]) > len(first.files["fact_sales"])
    facts = {t.name for t in data.contract.facts}
    rows = {r.table: r.row_count or 0 for r in second.stats if r.table in facts}
    assert sum(rows.values()) > sum(r.row_count or 0 for r in first.stats if r.table in facts)
    assert _count(catalog_setup, "fact_sales") == rows["fact_sales"] >= sales_before


def test_invalid_files_are_rejected_and_stay_invisible(catalog_setup: CatalogSetup) -> None:
    first = _refresh(catalog_setup)
    sales = _count(catalog_setup, "fact_sales")
    _add_duplicate_sales_file(catalog_setup.data.root)

    rejected = _refresh(catalog_setup)

    assert rejected.status == "rejected"
    assert rejected.report is not None and not rejected.report.ok
    assert any(i.table == "fact_sales" for i in rejected.report.issues)
    assert rejected.version == first.version
    assert _count(catalog_setup, "fact_sales") == sales  # views are pinned to v1's files

    con = open_catalog(catalog_setup.config)
    try:
        assert active_version(con) == first.version
        latest = refresh_history(con, limit=1)[0]
    finally:
        con.close()
    assert latest.status == "rejected" and latest.issue_count


def test_allow_invalid_activates_unvalidated_version(catalog_setup: CatalogSetup) -> None:
    _refresh(catalog_setup)
    sales = _count(catalog_setup, "fact_sales")
    _add_duplicate_sales_file(catalog_setup.data.root)

    result = _refresh(catalog_setup, allow_invalid=True)

    assert result.status == "active"
    assert result.version is not None and not result.version.validated
    assert _count(catalog_setup, "fact_sales") == sales + 5


def test_missing_table_is_rejected(catalog_setup: CatalogSetup) -> None:
    for path in (catalog_setup.data.root / "dim_part").iterdir():
        path.unlink()

    result = _refresh(catalog_setup)

    assert result.status == "rejected"
    assert result.report is not None
    assert any(i.table == "dim_part" and i.check == "files" for i in result.report.issues)
    assert result.version is None


def test_error_is_recorded_not_raised(catalog_setup: CatalogSetup) -> None:
    catalog_setup.config.paths.catalog_path.mkdir(parents=True)  # a directory, not a file

    result = _refresh(catalog_setup)

    assert result.status == "error"
    assert result.message
    assert result.version is None


def test_hooks_run_after_activation_only(catalog_setup: CatalogSetup) -> None:
    calls: list[str] = []

    def record(result: RefreshResult, config: object) -> None:
        calls.append(result.status)

    def broken(result: RefreshResult, config: object) -> None:
        raise RuntimeError("hook bug")

    hooks = [broken, record]
    contract, datasets = catalog_setup.data.contract, catalog_setup.datasets
    first = refresh(catalog_setup.config, contract, datasets, hooks=hooks)
    refresh(catalog_setup.config, contract, datasets, hooks=hooks)

    assert first.status == "active"  # a failing hook does not undo the activation
    assert calls == ["active"]


def test_fingerprint_is_order_independent_and_sensitive() -> None:
    when = datetime(2026, 1, 1)
    a, b = FileInfo("/d/a.parquet", 10, when), FileInfo("/d/b.parquet", 20, when)
    assert fingerprint({"t": [a, b], "u": []}) == fingerprint({"u": [], "t": [b, a]})
    assert fingerprint({"t": [a]}) != fingerprint({"t": [FileInfo(a.uri, 11, when)]})
    later = when + timedelta(seconds=1)
    assert fingerprint({"t": [a]}) != fingerprint({"t": [FileInfo(a.uri, 10, later)]})
    assert fingerprint({"t": [a]}) != fingerprint({"u": [a]})
