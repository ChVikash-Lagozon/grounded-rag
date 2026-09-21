"""``carq`` command-line entry point. Later phases add their subcommands here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, NoReturn

import duckdb
import typer
import yaml

from carquery import __version__
from carquery.catalog import (
    CatalogError,
    DataVersion,
    TableStats,
    active_version,
    open_catalog,
    refresh_history,
    table_stats,
)
from carquery.config import AppConfig, ConfigError, load_config, redact
from carquery.contract import SchemaContract, load_contract, schema_markdown
from carquery.datasets import Dataset, load_datasets
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
from carquery.refresh import connect_scanned, refresh, scan
from carquery.storage import get_connector
from carquery.validation import validate, validate_views

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
    """Validate the files the storage connector sees (not the catalog); exit 1 on errors."""
    config = _load()
    contract = _load_contract()
    connector = get_connector(config)
    con = connect_scanned(connector, scan(connector, _load_datasets(contract)))
    try:
        report = validate_views(con, contract)
    finally:
        con.close()
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(code=1)


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


# --------------------------------------------------------------------------------------
# Catalog: refresh, status, sql; storage check
# --------------------------------------------------------------------------------------

catalog_app = typer.Typer(help="The DuckDB catalog the queries run on.", no_args_is_help=True)
storage_app = typer.Typer(help="The storage the data files are read from.", no_args_is_help=True)
app.add_typer(catalog_app, name="catalog")
app.add_typer(storage_app, name="storage")


def _load_datasets(contract: SchemaContract) -> list[Dataset]:
    try:
        return load_datasets(contract)
    except ConfigError as exc:
        _fail(f"Configuration error: {exc}")


def _echo_stats(stats: list[TableStats]) -> None:
    typer.echo(f"  {'table':<24} {'files':>6} {'rows':>12}  dates")
    for s in stats:
        rows = f"{s.row_count:,}" if s.row_count is not None else "?"
        dates = f"{s.min_date} .. {s.max_date}" if s.max_date else ""
        typer.echo(f"  {s.table:<24} {s.file_count:>6} {rows:>12}  {dates}")


def _echo_version(version: DataVersion) -> None:
    typer.echo(f"Active version: {version.label}")
    typer.echo(f"  data through: {version.data_through}   validated: {version.validated}")
    typer.echo(f"  source: {version.connector}   created: {version.created_at:%Y-%m-%d %H:%M} UTC")


@app.command("refresh")
def refresh_cmd(
    force: Annotated[
        bool, typer.Option("--force", help="Create a new version even if no files changed.")
    ] = False,
    allow_invalid: Annotated[
        bool,
        typer.Option("--allow-invalid", help="Activate even if validation fails (not validated)."),
    ] = False,
) -> None:
    """Scan storage, validate, and activate a new data version; exit 1 if rejected or failed."""
    config = _load()
    contract = _load_contract()
    result = refresh(
        config, contract, _load_datasets(contract), force=force, allow_invalid=allow_invalid
    )
    if result.report and result.report.issues:
        typer.echo(result.report.render())
    if result.status == "active":
        assert result.version is not None
        typer.secho(f"Refresh activated {result.version.label}", fg=typer.colors.GREEN)
        typer.echo(f"  data through: {result.version.data_through}")
        if not result.version.validated:
            typer.secho("  WARNING: activated without passing validation", fg=typer.colors.YELLOW)
        _echo_stats(result.stats)
        return
    current = result.version.label if result.version else "none"
    if result.status == "no_change":
        typer.echo(f"No change ({result.message}); active version: {current}")
        return
    _fail(f"Refresh {result.status}: {result.message}. Active version stays: {current}")


@catalog_app.command("status")
def catalog_status_cmd() -> None:
    """Show the active data version, its tables and the latest refreshes."""
    config = _load()
    try:
        con = open_catalog(config)
    except CatalogError as exc:
        _fail(str(exc))
    try:
        version = active_version(con)
        history = refresh_history(con, config.catalog.history_limit)
        stats = table_stats(con, version.version_id) if version else []
    finally:
        con.close()
    order = {name: i for i, name in enumerate(_load_contract().tables)}
    stats.sort(key=lambda s: order.get(s.table, len(order)))
    if version is None:
        typer.echo("No active version yet; run `carq refresh`.")
    else:
        _echo_version(version)
        _echo_stats(stats)
    if history:
        typer.echo("Latest refreshes:")
    for entry in history:
        version_text = f"v{entry.version_id}" if entry.version_id else "-"
        typer.echo(
            f"  {entry.started_at:%Y-%m-%d %H:%M:%S}  {entry.status:<10} {version_text:<5} "
            f"{entry.message or ''}"
        )


@catalog_app.command("sql")
def catalog_sql_cmd(
    query: Annotated[str, typer.Argument(help="SQL to run on the catalog (read-only).")],
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows to print.")] = 50,
) -> None:
    """Run a query on the catalog views (dev convenience; Phase 3 adds the guarded engine)."""
    config = _load()
    try:
        con = open_catalog(config)
    except CatalogError as exc:
        _fail(str(exc))
    try:
        relation = con.sql(query)
        if relation is None:
            typer.echo("OK")
            return
        rows = relation.fetchmany(limit + 1)
        _echo_rows(relation.columns, rows[:limit])
        if len(rows) > limit:
            typer.echo(f"(first {limit} rows shown; use --limit for more)")
    except duckdb.Error as exc:
        _fail(f"Query failed: {exc}")
    finally:
        con.close()


def _echo_rows(columns: list[str], rows: list[tuple[object, ...]]) -> None:
    """A plain-text table (ASCII only, so any console encoding can print it)."""
    cells = [["NULL" if v is None else str(v) for v in row] for row in rows]
    widths = [max([len(c), *(len(r[i]) for r in cells)]) for i, c in enumerate(columns)]
    typer.echo("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    typer.echo("  ".join("-" * w for w in widths))
    for row in cells:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
    typer.echo(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


@storage_app.command("check")
def storage_check_cmd() -> None:
    """Show the configured storage and the files it holds per dataset."""
    config = _load()
    contract = _load_contract()
    connector = get_connector(config)
    typer.echo(f"Storage: {connector.describe()}")
    missing = 0
    for dataset in _load_datasets(contract):
        files = connector.list_files(dataset)
        missing += not files
        size = sum(f.size for f in files) / 1_048_576
        typer.echo(
            f"  {dataset.name:<24} {len(files):>6} files {size:>9.1f} MiB  "
            f"{dataset.path}/{dataset.pattern}"
        )
    if missing:
        _fail(f"{missing} dataset(s) have no files")


if __name__ == "__main__":
    app()
