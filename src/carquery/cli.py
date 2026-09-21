"""``carq`` command-line entry point. Later phases add their subcommands here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml

from carquery import __version__
from carquery.config import AppConfig, ConfigError, load_config, redact
from carquery.contract import SchemaContract, load_contract, schema_markdown
from carquery.generator import (
    GenerationError,
    GenerationResult,
    generate_day,
    generate_history,
    load_generator_config,
    load_reference,
)
from carquery.generator.config import GeneratorConfig, ReferenceData
from carquery.logging import configure_logging, get_logger
from carquery.profile import build_profile
from carquery.validation import validate

app = typer.Typer(
    help="Natural-language queries over car manufacturer parquet data.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"carq {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """carq: ask questions about the data in plain English."""


def _load() -> AppConfig:
    try:
        config = load_config()
    except ConfigError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    configure_logging(config.logging)
    return config


@config_app.command("show")
def config_show(
    fmt: Annotated[str, typer.Option("--format", help="Output format: yaml or json.")] = "yaml",
) -> None:
    """Print the resolved configuration, with secrets redacted."""
    data = redact(_load())
    if fmt == "json":
        typer.echo(json.dumps(data, indent=2))
    elif fmt == "yaml":
        typer.echo(yaml.safe_dump(data, sort_keys=False).rstrip())
    else:
        raise typer.BadParameter("must be 'yaml' or 'json'", param_hint="--format")


@config_app.command("validate")
def config_validate() -> None:
    """Load and validate the configuration; exit code 1 on error."""
    config = _load()
    get_logger(__name__).debug("config_validated", environment=config.environment)
    typer.secho("Configuration is valid.", fg=typer.colors.GREEN)


# --------------------------------------------------------------------------------------
# Data: generate, validate, profile, schema
# --------------------------------------------------------------------------------------

generate_app = typer.Typer(help="Generate synthetic data.", no_args_is_help=True)
schema_app = typer.Typer(help="Schema contract documentation.", no_args_is_help=True)
app.add_typer(generate_app, name="generate")
app.add_typer(schema_app, name="schema")

PresetOption = Annotated[
    str | None, typer.Option("--preset", help="Scale preset from generator.yaml.")
]
SeedOption = Annotated[int | None, typer.Option("--seed", help="Random seed (default: config).")]


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _load_contract() -> SchemaContract:
    try:
        return load_contract()
    except ConfigError as exc:
        _fail(f"Configuration error: {exc}")


def _load_generator(preset: str | None, seed: int | None) -> tuple[GeneratorConfig, ReferenceData]:
    try:
        return load_generator_config(preset=preset, seed=seed), load_reference()
    except ConfigError as exc:
        _fail(f"Configuration error: {exc}")


def _report_generation(result: GenerationResult) -> None:
    written = {name: rows for name, rows in result.rows.items() if rows}
    typer.echo(f"Wrote data as of {result.as_of}: {sum(written.values()):,} rows")
    for name, rows in written.items():
        typer.echo(f"  {name:<24} {rows:>10,}")
    if result.ground_truth:
        typer.echo(f"Ground truth: {result.ground_truth}")
    for key, metrics in (result.patterns or {}).items():
        mark = "detected" if metrics.get("detected") else "NOT DETECTED"
        typer.echo(f"  {key:<24} {mark}")


def _validate_or_fail(data_root: Path, contract: SchemaContract) -> None:
    report = validate(data_root, contract)
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(code=1)


@generate_app.command("history")
def generate_history_cmd(
    preset: PresetOption = None,
    seed: SeedOption = None,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace existing data in data_root.")
    ] = False,
) -> None:
    """Generate the full history into data_root, then validate it."""
    config = _load()
    contract = _load_contract()
    gen, ref = _load_generator(preset, seed)
    try:
        result = generate_history(gen, ref, contract, config.paths.data_root, overwrite)
    except GenerationError as exc:
        _fail(str(exc))
    _report_generation(result)
    _validate_or_fail(config.paths.data_root, contract)


@generate_app.command("day")
def generate_day_cmd(
    day: Annotated[
        int, typer.Argument(help="Day number after the history end date (1 = next day).")
    ],
    preset: PresetOption = None,
    seed: SeedOption = None,
) -> None:
    """Append the files for day N after the history (dimensions are rewritten), then validate."""
    config = _load()
    contract = _load_contract()
    gen, ref = _load_generator(preset, seed)
    try:
        result = generate_day(gen, ref, contract, config.paths.data_root, day)
    except GenerationError as exc:
        _fail(str(exc))
    _report_generation(result)
    _validate_or_fail(config.paths.data_root, contract)


@app.command("validate")
def validate_cmd() -> None:
    """Validate the data in data_root against the schema contract; exit code 1 on errors."""
    config = _load()
    _validate_or_fail(config.paths.data_root, _load_contract())


@app.command("profile")
def profile_cmd(
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output file (default: <docs_dir>/data_profile.md)."),
    ] = None,
) -> None:
    """Write the data profile report (row counts, null rates, distributions)."""
    config = _load()
    target = out or config.paths.docs_dir / "data_profile.md"
    target.write_text(build_profile(config.paths.data_root, _load_contract()), encoding="utf-8")
    typer.echo(f"Wrote {target}")


@schema_app.command("erd")
def schema_erd_cmd(
    out: Annotated[
        Path | None, typer.Option("--out", help="Output file (default: <docs_dir>/schema.md).")
    ] = None,
) -> None:
    """Write the ER diagram (Mermaid) and column documentation from the schema contract."""
    config = _load()
    target = out or config.paths.docs_dir / "schema.md"
    target.write_text(schema_markdown(_load_contract()), encoding="utf-8")
    typer.echo(f"Wrote {target}")


if __name__ == "__main__":
    app()
