from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from carquery.catalog import CatalogError, active_version, connect_catalog, open_catalog
from carquery.cli import app
from carquery.refresh import refresh

from .conftest import CatalogSetup

runner = CliRunner()


def _cli_env(config_dir: Path, setup: CatalogSetup) -> dict[str, str]:
    return {"CARQ_CONFIG_DIR": str(config_dir), "DATA_ROOT": str(setup.data.root)}


def test_read_only_open_without_catalog_fails_clearly(catalog_setup: CatalogSetup) -> None:
    with pytest.raises(CatalogError, match="carq refresh"):
        open_catalog(catalog_setup.config)


def test_read_only_connection_sees_views_but_cannot_write(catalog_setup: CatalogSetup) -> None:
    refresh(catalog_setup.config, catalog_setup.data.contract, catalog_setup.datasets, hooks=[])
    con = open_catalog(catalog_setup.config)
    try:
        assert active_version(con) is not None
        views = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views()").fetchall()}
        assert set(catalog_setup.data.contract.tables) <= views
        with pytest.raises(duckdb.Error):
            con.execute("DELETE FROM _data_version")
    finally:
        con.close()


def test_writer_in_another_process_blocks_until_timeout(catalog_setup: CatalogSetup) -> None:
    path = catalog_setup.config.paths.catalog_path
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import sys, duckdb
                con = duckdb.connect({str(path)!r})
                print("locked", flush=True)
                sys.stdin.read()
                """
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(CatalogError, match="locked"):
            connect_catalog(path, read_only=False, busy_timeout=0.3)

        # the holder exits after a moment; a writer with enough patience gets in
        assert holder.stdin is not None
        release = threading.Timer(0.5, holder.stdin.close)
        release.start()
        connect_catalog(path, read_only=False, busy_timeout=20).close()
        release.join()
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_cli_refresh_status_sql_and_storage(
    data_config_dir: Path, catalog_setup: CatalogSetup
) -> None:
    env = _cli_env(data_config_dir, catalog_setup)

    missing = runner.invoke(app, ["catalog", "status"], env=env)
    assert missing.exit_code == 1
    assert "carq refresh" in missing.output

    check = runner.invoke(app, ["storage", "check"], env=env)
    assert check.exit_code == 0, check.output
    assert "fact_sales" in check.output and "local:" in check.output

    first = runner.invoke(app, ["refresh"], env=env)
    assert first.exit_code == 0, first.output
    assert "Refresh activated v1" in first.output

    again = runner.invoke(app, ["refresh"], env=env)
    assert again.exit_code == 0, again.output
    assert "No change" in again.output

    status = runner.invoke(app, ["catalog", "status"], env=env)
    assert status.exit_code == 0, status.output
    assert "Active version: v1" in status.output
    assert "no_change" in status.output and "fact_warranty_claim" in status.output

    sql = runner.invoke(
        app, ["catalog", "sql", "SELECT count(*) AS n FROM dim_date", "--limit", "5"], env=env
    )
    assert sql.exit_code == 0, sql.output
    assert "n" in sql.output and "(1 row)" in sql.output

    bad = runner.invoke(app, ["catalog", "sql", "SELECT * FROM nope"], env=env)
    assert bad.exit_code == 1
    assert "Query failed" in bad.output

    assert runner.invoke(app, ["validate"], env=env).exit_code == 0


def test_cli_refresh_rejected_exits_1(data_config_dir: Path, catalog_setup: CatalogSetup) -> None:
    env = _cli_env(data_config_dir, catalog_setup)
    for path in (catalog_setup.data.root / "dim_part").iterdir():
        path.unlink()

    result = runner.invoke(app, ["refresh"], env=env)

    assert result.exit_code == 1
    assert "Refresh rejected" in result.output
    assert runner.invoke(app, ["storage", "check"], env=env).exit_code == 1
    assert runner.invoke(app, ["validate"], env=env).exit_code == 1
