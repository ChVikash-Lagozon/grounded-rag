"""Entry points: generate the full history, or the files for day N after the history.

Both run the same deterministic simulation of the whole timeline (history start to
``end_date + day_horizon_days``) and differ only in what they write:

- history: every row that has arrived by ``end_date``, and dimension snapshots as of then;
- day N: only the rows arriving on ``end_date + N`` (appended as new files), and dimension
  snapshots as of that day (overwritten).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import yaml

from carquery.contract import SchemaContract
from carquery.datafiles import connect_views, table_files
from carquery.generator.aftersales import (
    RecallCampaign,
    recall_campaign_table,
    recall_campaigns,
    simulate_aftersales,
)
from carquery.generator.calendar import build_dim_date, to_date
from carquery.generator.config import GeneratorConfig, ReferenceData
from carquery.generator.patterns import build_patterns, measure
from carquery.generator.vehicles import (
    Counters,
    chunk_days,
    fleet_customer_table,
    production_schedule,
    simulate_production,
    simulate_sales,
)
from carquery.generator.world import World, build_world, dimension_tables, supply_table
from carquery.generator.writer import Staging, write_all
from carquery.logging import get_logger

GROUND_TRUTH_FILE = "ground_truth.yaml"
log = get_logger(__name__)


class GenerationError(Exception):
    """Raised when generation cannot run (existing data, mismatched settings, bad day)."""


@dataclass
class GenerationResult:
    as_of: date
    files: dict[str, list[Path]]
    rows: dict[str, int]
    ground_truth: Path | None = None
    patterns: dict[str, dict[str, Any]] | None = None


def dataset_fingerprint(cfg: GeneratorConfig) -> dict[str, Any]:
    return {
        "seed": cfg.seed,
        "preset": cfg.preset,
        "end_date": cfg.end_date.isoformat(),
        "history_years": cfg.history_years,
        "day_horizon_days": cfg.day_horizon_days,
    }


def generate_history(
    cfg: GeneratorConfig,
    ref: ReferenceData,
    contract: SchemaContract,
    data_root: Path,
    overwrite: bool = False,
) -> GenerationResult:
    existing = [name for name in contract.tables if table_files(data_root, name)]
    if existing and not overwrite:
        raise GenerationError(
            f"{data_root} already contains data ({', '.join(existing[:3])}, ...); "
            "use --overwrite to replace it"
        )
    for name in contract.tables:
        shutil.rmtree(data_root / name, ignore_errors=True)
    (data_root / GROUND_TRUTH_FILE).unlink(missing_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    log.info("generation_started", mode="history", **dataset_fingerprint(cfg))
    world, staging, campaigns = _simulate(cfg, ref, data_root)
    try:
        files = write_all(
            staging,
            contract,
            data_root,
            cfg.end_date,
            "history",
            cfg.active_preset.row_group_size,
            cfg.active_preset.partitioned_tables,
        )
    finally:
        staging.cleanup()

    patterns = build_patterns(world, campaigns)
    con = connect_views(data_root, contract)
    try:
        for pattern in patterns:
            pattern.measured = measure(con, pattern)
    finally:
        con.close()
    ground_truth = _write_ground_truth(data_root, cfg, world, patterns)
    result = GenerationResult(
        as_of=cfg.end_date,
        files=files,
        rows=_row_counts(files),
        ground_truth=ground_truth,
        patterns={p.key: p.measured for p in patterns},
    )
    log.info("generation_finished", mode="history", rows=sum(result.rows.values()))
    return result


def generate_day(
    cfg: GeneratorConfig,
    ref: ReferenceData,
    contract: SchemaContract,
    data_root: Path,
    day: int,
) -> GenerationResult:
    if not 1 <= day <= cfg.day_horizon_days:
        raise GenerationError(
            f"day must be between 1 and {cfg.day_horizon_days} (day_horizon_days)"
        )
    truth_path = data_root / GROUND_TRUTH_FILE
    if not truth_path.is_file():
        raise GenerationError(f"No history found in {data_root}; run `carq generate history` first")
    recorded = yaml.safe_load(truth_path.read_text(encoding="utf-8")).get("dataset", {})
    expected = dataset_fingerprint(cfg)
    mismatched = {k: (recorded.get(k), v) for k, v in expected.items() if recorded.get(k) != v}
    if mismatched:
        detail = ", ".join(f"{k}: history={a!r} now={b!r}" for k, (a, b) in mismatched.items())
        raise GenerationError(f"Settings differ from the generated history ({detail})")

    as_of = cfg.end_date + timedelta(days=day)
    log.info("generation_started", mode="day", day=day, as_of=as_of.isoformat())
    _, staging, _ = _simulate(cfg, ref, data_root)
    try:
        files = write_all(
            staging,
            contract,
            data_root,
            as_of,
            "day",
            cfg.active_preset.row_group_size,
            cfg.active_preset.partitioned_tables,
        )
    finally:
        staging.cleanup()
    result = GenerationResult(as_of=as_of, files=files, rows=_row_counts(files))
    log.info("generation_finished", mode="day", day=day, rows=sum(result.rows.values()))
    return result


def _simulate(
    cfg: GeneratorConfig, ref: ReferenceData, data_root: Path
) -> tuple[World, Staging, list[RecallCampaign]]:
    """Simulate the whole timeline into the staging area."""
    world = build_world(cfg, ref)
    staging = Staging(data_root)
    try:
        tl = world.timeline
        first = to_date(tl.history_start - cfg.supply.lead_days)
        staging.add("dim_date", build_dim_date(first, to_date(tl.horizon_end)))
        for name, table in dimension_tables(world).items():
            staging.add(name, table)
        staging.add("fact_part_supply", supply_table(world))
        campaigns = recall_campaigns(world)
        staging.add("dim_recall_campaign", recall_campaign_table(world, campaigns))

        days, counts = production_schedule(world)
        counters = Counters()
        for chunk, index in enumerate(chunk_days(world, days)):
            build = np.repeat(days[index], counts[index])
            if not len(build):
                continue
            vehicles, tables = simulate_production(world, chunk, build, counters)
            tables |= simulate_sales(world, chunk, vehicles, counters)
            tables |= simulate_aftersales(world, chunk, vehicles, campaigns, counters)
            for name, table in tables.items():
                staging.add("dim_customer" if name == "_individual_customers" else name, table)
            log.debug("chunk_simulated", chunk=chunk, vehicles=len(build))
        staging.add("dim_customer", fleet_customer_table(world))
    except BaseException:
        staging.cleanup()
        raise
    return world, staging, campaigns


def _row_counts(files: dict[str, list[Path]]) -> dict[str, int]:
    return {
        name: sum(pq.ParquetFile(f).metadata.num_rows for f in paths)
        for name, paths in files.items()
    }


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str_presenter)


def _write_ground_truth(
    data_root: Path, cfg: GeneratorConfig, world: World, patterns: list[Any]
) -> Path:
    tl = world.timeline
    document = {
        "dataset": dataset_fingerprint(cfg),
        "history": {
            "start": str(to_date(tl.history_start)),
            "end": str(to_date(tl.end)),
            "day_horizon_end": str(to_date(tl.horizon_end)),
        },
        "note": (
            "Generated by `carq generate history`. Detection SQL runs against views named "
            "after the tables. `measured` is the result on this dataset."
        ),
        "patterns": {
            p.key: {
                "title": p.title,
                "description": p.description,
                "parameters": p.parameters,
                "expectation": p.expectation,
                "measured": p.measured,
                "detection_sql": p.detection_sql.strip() + "\n",
            }
            for p in patterns
        },
    }
    path = data_root / GROUND_TRUTH_FILE
    path.write_text(
        yaml.dump(document, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return path
