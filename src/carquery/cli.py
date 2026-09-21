"""``carq`` command-line entry point. Later phases add their subcommands here."""

from __future__ import annotations

import json
from typing import Annotated

import typer
import yaml

from carquery import __version__
from carquery.config import AppConfig, ConfigError, load_config, redact
from carquery.logging import configure_logging, get_logger

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


if __name__ == "__main__":
    app()
