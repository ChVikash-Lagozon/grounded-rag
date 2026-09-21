from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from carquery import __version__
from carquery.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_config_show_prints_resolved_yaml(config_dir: Path) -> None:
    result = runner.invoke(
        app, ["config", "show"], env={"CARQ_CONFIG_DIR": str(config_dir), "CARQ_ENV": "test"}
    )
    assert result.exit_code == 0, result.output

    shown = yaml.safe_load(result.output)
    assert shown["environment"] == "test"
    assert Path(shown["paths"]["data_root"]).is_absolute()


def test_config_show_json(config_dir: Path) -> None:
    result = runner.invoke(
        app, ["config", "show", "--format", "json"], env={"CARQ_CONFIG_DIR": str(config_dir)}
    )
    assert result.exit_code == 0, result.output
    assert '"logging"' in result.output


def test_config_validate_ok(config_dir: Path) -> None:
    result = runner.invoke(app, ["config", "validate"], env={"CARQ_CONFIG_DIR": str(config_dir)})
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_config_validate_fails_on_bad_value(config_dir: Path) -> None:
    result = runner.invoke(
        app,
        ["config", "validate"],
        env={"CARQ_CONFIG_DIR": str(config_dir), "CARQ__LOGGING__FORMAT": "xml"},
    )
    assert result.exit_code == 1
