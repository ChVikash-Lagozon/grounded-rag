from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from carquery.cli import app

runner = CliRunner()


def _env(config_dir: Path, data_root: Path) -> dict[str, str]:
    return {
        "CARQ_CONFIG_DIR": str(config_dir),
        "DATA_ROOT": str(data_root),
        "GEN_PRESET": "test",
    }


def test_generate_history_day_validate_profile(data_config_dir: Path, tmp_path: Path) -> None:
    env = _env(data_config_dir, tmp_path / "data")

    result = runner.invoke(app, ["generate", "history"], env=env)
    assert result.exit_code == 0, result.output
    assert "Validation PASSED" in result.output
    assert "p1_brake_batch" in result.output
    assert "NOT DETECTED" not in result.output

    again = runner.invoke(app, ["generate", "history"], env=env)
    assert again.exit_code == 1
    assert "--overwrite" in again.output

    day = runner.invoke(app, ["generate", "day", "1"], env=env)
    assert day.exit_code == 0, day.output
    assert "Wrote data as of 2026-09-01" in day.output

    assert runner.invoke(app, ["validate"], env=env).exit_code == 0

    out = tmp_path / "profile.md"
    profile = runner.invoke(app, ["profile", "--out", str(out)], env=env)
    assert profile.exit_code == 0, profile.output
    text = out.read_text(encoding="utf-8")
    assert "| `fact_sales` | fact |" in text
    assert "preset `test`" in text


def test_generate_day_out_of_range(data_config_dir: Path, tmp_path: Path) -> None:
    env = _env(data_config_dir, tmp_path / "data")
    result = runner.invoke(app, ["generate", "day", "1"], env=env)
    assert result.exit_code == 1
    assert "No history found" in result.output


def test_unknown_preset(data_config_dir: Path, tmp_path: Path) -> None:
    env = _env(data_config_dir, tmp_path / "data")
    result = runner.invoke(app, ["generate", "history", "--preset", "huge"], env=env)
    assert result.exit_code == 1
    assert "Unknown preset" in result.output


def test_validate_empty_data_root_fails(data_config_dir: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate"], env=_env(data_config_dir, tmp_path / "empty"))
    assert result.exit_code == 1
    assert "no parquet files found" in result.output


def test_schema_erd(data_config_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "schema.md"
    result = runner.invoke(
        app, ["schema", "erd", "--out", str(out)], env=_env(data_config_dir, tmp_path)
    )
    assert result.exit_code == 0, result.output
    assert "```mermaid\nerDiagram" in out.read_text(encoding="utf-8")
