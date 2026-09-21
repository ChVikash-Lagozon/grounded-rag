from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest

from carquery.catalog import active_version, open_catalog
from carquery.config import AppConfig, load_config
from carquery.query import QueryEngine, QueryError, check_statement
from carquery.refresh import refresh

from .conftest import CatalogSetup

SLOW = "SELECT count(*) FROM range(100000000000) a WHERE a.range % 7 = 99"


def _with_limits(config: AppConfig, **limits: object) -> AppConfig:
    return config.model_copy(update={"query": config.query.model_copy(update=limits)})


@pytest.fixture
def engine(refreshed_catalog: AppConfig) -> QueryEngine:
    return QueryEngine(refreshed_catalog)


def _error(engine: QueryEngine, sql: str) -> QueryError:
    with pytest.raises(QueryError) as info:
        engine.execute(sql)
    return info.value


# --------------------------------------------------------------------------------------
# Statement checks
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "SELECT 1"),
        ("  SELECT 1 ;  ", "SELECT 1"),
        ("SELECT 1;;", "SELECT 1"),
        ("SELECT 1; -- trailing", "SELECT 1"),
        ("SELECT 1 /* c */ ;", "SELECT 1 /* c */"),
        ("SELECT 1 -- no semicolon", "SELECT 1 -- no semicolon"),
        ("SELECT '--;' AS x;", "SELECT '--;' AS x"),
        (
            "-- lead\nWITH a AS (SELECT 1) SELECT * FROM a",
            "-- lead\nWITH a AS (SELECT 1) SELECT * FROM a",
        ),
        ("FROM dim_plant", "FROM dim_plant"),
    ],
)
def test_check_statement_accepts_and_strips(sql: str, expected: str) -> None:
    assert check_statement(sql, 1000) == expected


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("", "empty"),
        ("   -- only a comment", "empty"),
        ("SELECT 1; SELECT 2", "multiple_statements"),
        ("INSERT INTO t VALUES (1)", "not_read_only"),
        ("PRAGMA version", "not_read_only"),
        ("/* hidden */ pragma database_list", "not_read_only"),
        ("CALL pragma_version()", "not_read_only"),
        ("PIVOT fact_sales ON channel USING count(*)", "multiple_statements"),
        ("SELEC 1", "syntax"),
        ("SELECT (1", "syntax"),
    ],
)
def test_check_statement_rejects(sql: str, kind: str) -> None:
    with pytest.raises(QueryError) as info:
        check_statement(sql, 1000)
    assert info.value.kind == kind


def test_check_statement_length_limit() -> None:
    with pytest.raises(QueryError) as info:
        check_statement("SELECT " + "1 + " * 50 + "1", 100)
    assert info.value.kind == "too_long" and "100" in (info.value.hint or "")


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


def test_join_aggregate_result(engine: QueryEngine, refreshed_catalog: AppConfig) -> None:
    result = engine.execute(
        "SELECT m.powertrain, count(*) AS units, min(s.sale_date) AS first_sale\n"
        "FROM fact_sales s JOIN fact_production p USING (vin)\n"
        "JOIN dim_vehicle_model m USING (model_id)\n"
        "GROUP BY 1 ORDER BY units DESC; -- units by powertrain"
    )
    assert result.columns == ["powertrain", "units", "first_sale"]
    assert result.types == ["VARCHAR", "BIGINT", "DATE"]
    units = [row[1] for row in result.rows]
    assert units == sorted(units, reverse=True) and sum(units) > 0
    assert isinstance(result.rows[0][2], date)
    assert not result.truncated and result.row_count == len(result.rows)
    assert result.to_records()[0]["powertrain"] == result.rows[0][0]
    assert result.duration_ms > 0 and len(result.query_id) == 12

    con = open_catalog(refreshed_catalog)
    try:
        assert result.data_version == active_version(con)
    finally:
        con.close()


def test_order_is_kept_through_the_wrapper(engine: QueryEngine) -> None:
    result = engine.execute("SELECT calendar_date FROM dim_date ORDER BY calendar_date DESC")
    dates = [row[0] for row in result.rows]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.parametrize(
    "sql",
    [
        "DESCRIBE fact_sales",
        "SUMMARIZE dim_plant",
        "SHOW TABLES",
        "FROM dim_plant",
        "WITH p AS (SELECT * FROM dim_plant) SELECT count(*) FROM p",
        "SELECT * FROM dim_plant UNION ALL SELECT * FROM dim_plant",
        "SELECT count(*) FROM _data_version",
    ],
)
def test_read_only_statement_forms_work(engine: QueryEngine, sql: str) -> None:
    assert engine.execute(sql).row_count > 0


def test_truncation_and_limit_clamping(refreshed_catalog: AppConfig) -> None:
    engine = QueryEngine(_with_limits(refreshed_catalog, max_rows=10))

    result = engine.execute("SELECT * FROM dim_date", max_rows=3)
    assert result.row_count == 3 and result.truncated and result.max_rows == 3

    clamped = engine.execute("SELECT * FROM dim_date", max_rows=10_000)
    assert clamped.row_count == 10 and clamped.max_rows == 10

    exact = engine.execute("SELECT * FROM dim_date LIMIT 10")
    assert exact.row_count == 10 and not exact.truncated

    with pytest.raises(ValueError):
        engine.execute("SELECT 1", max_rows=-1)


def test_duplicate_output_names_are_suffixed(engine: QueryEngine) -> None:
    assert engine.execute("SELECT 1 AS a, 2 AS a").columns == ["a", "a_1"]


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


def test_unknown_table_lists_tables(engine: QueryEngine) -> None:
    error = _error(engine, "SELECT * FROM fact_sale")
    assert error.kind == "unknown_table" and error.retryable
    assert "fact_sales" in error.message  # DuckDB's "Did you mean"
    assert error.hint and "dim_vehicle_model" in error.hint
    assert error.sql == "SELECT * FROM fact_sale"


def test_unknown_column_points_at_the_callers_line(engine: QueryEngine) -> None:
    error = _error(engine, "SELECT nope FROM fact_sales")
    assert error.kind == "unknown_column"
    assert "LINE 1:" in error.message and "Candidate bindings" in error.message
    llm = error.to_llm()
    assert llm.startswith("Query error (unknown_column):") and "Hint:" in llm
    assert error.to_dict()["retryable"] is True


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT vin FROM fact_sales s JOIN fact_production p ON s.vin = p.vin", "binder"),
        ("SELECT sale_type, count(*) FROM fact_sales", "binder"),
        ("SELECT now() AS ts, current_date AS d", None),
        ("SELECT DATE '2025-13-01'", "type_error"),
        ("SELECT 'x'::INT", "type_error"),
        ("SELECT getenv('PATH')", "binder"),
        ("SELECT error('boom')", "execution"),
    ],
)
def test_error_kinds(engine: QueryEngine, sql: str, kind: str | None) -> None:
    if kind is None:  # control case: must succeed (TIMESTAMPTZ results need pytz)
        assert engine.execute(sql).row_count == 1
        return
    error = _error(engine, sql)
    assert error.kind == kind, error.message
    assert error.retryable


def test_validate_reports_without_raising(engine: QueryEngine) -> None:
    assert engine.validate("SELECT count(*) FROM fact_sales") is None
    error = engine.validate("SELECT nope FROM fact_sales")
    assert error is not None and error.kind == "unknown_column"
    rejected = engine.validate("DROP TABLE fact_sales")
    assert rejected is not None and rejected.kind == "not_read_only"


def test_runtime_errors_without_explain(refreshed_catalog: AppConfig) -> None:
    engine = QueryEngine(_with_limits(refreshed_catalog, explain_before_execute=False))
    assert _error(engine, "SELECT nope FROM fact_sales").kind == "unknown_column"
    assert engine.execute("SELECT count(*) FROM dim_plant").rows[0][0] > 0


def test_timeout_cancels_and_engine_stays_usable(engine: QueryEngine) -> None:
    started = time.monotonic()
    with pytest.raises(QueryError) as info:
        engine.execute(SLOW, timeout=0.3)
    assert info.value.kind == "timeout" and info.value.retryable
    assert time.monotonic() - started < 5
    assert engine.execute("SELECT 42 AS x").rows == [(42,)]


def test_timeout_is_clamped_to_config(refreshed_catalog: AppConfig) -> None:
    engine = QueryEngine(_with_limits(refreshed_catalog, timeout_seconds=0.3))
    with pytest.raises(QueryError) as info:
        engine.execute(SLOW, timeout=60)
    assert info.value.kind == "timeout"


def test_no_catalog_is_unavailable(config_dir: Path, tmp_path: Path) -> None:
    config = load_config(config_dir, env={"DATA_ROOT": str(tmp_path / "empty")})
    error = _error(QueryEngine(config), "SELECT 1")
    assert error.kind == "unavailable" and not error.retryable
    assert "carq refresh" in (error.hint or "")


# --------------------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------------------


def test_parallel_queries(engine: QueryEngine) -> None:
    sqls = [f"SELECT count(*) + {i} FROM fact_sales" for i in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(engine.execute, sqls))
    base = results[0].rows[0][0]
    assert [r.rows[0][0] for r in results] == [base + i for i in range(8)]


def test_timeout_of_one_query_leaves_parallel_queries_alone(engine: QueryEngine) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(engine.execute, SLOW, timeout=0.5)
        fast = [engine.execute("SELECT count(*) FROM dim_plant") for _ in range(5)]
        with pytest.raises(QueryError) as info:
            slow.result()
    assert info.value.kind == "timeout"
    assert len({r.rows[0][0] for r in fast}) == 1


def test_in_process_refresh_waits_for_a_running_query(catalog_setup: CatalogSetup) -> None:
    setup = catalog_setup
    refresh(setup.config, setup.data.contract, setup.datasets, hooks=[])
    engine = QueryEngine(setup.config)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(engine.execute, SLOW, timeout=1.0)
        time.sleep(0.2)  # let the query open the catalog
        result = refresh(setup.config, setup.data.contract, setup.datasets, force=True, hooks=[])
        with pytest.raises(QueryError):
            running.result()
    assert result.status == "active", result.message
    assert result.version is not None and result.version.version_id == 2


def test_engine_does_not_block_refresh(catalog_setup: CatalogSetup) -> None:
    setup = catalog_setup
    first = refresh(setup.config, setup.data.contract, setup.datasets, hooks=[])
    engine = QueryEngine(setup.config)
    assert engine.execute("SELECT 1").data_version == first.version

    second = refresh(setup.config, setup.data.contract, setup.datasets, force=True, hooks=[])

    assert second.status == "active"
    assert engine.execute("SELECT 1").data_version == second.version
