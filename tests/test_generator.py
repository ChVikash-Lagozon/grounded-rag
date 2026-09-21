from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest
import yaml

from carquery.datafiles import connect_views, table_files
from carquery.generator import GROUND_TRUTH_FILE, GenerationError, generate_day, generate_history
from carquery.generator.calendar import (
    build_dim_date,
    fiscal_year,
    model_year,
    quarter_end,
    to_date,
    to_day,
)
from carquery.generator.runner import _simulate
from carquery.generator.writer import write_all
from carquery.validation import validate

from .conftest import Dataset, load_data_configs


def _days(*values: str) -> np.ndarray:
    return np.array([to_day(date.fromisoformat(v)) for v in values])


def test_fiscal_year_is_named_after_the_year_it_ends_in() -> None:
    assert list(fiscal_year(_days("2025-03-31", "2025-04-01", "2026-03-31"))) == [2025, 2026, 2026]


def test_quarter_end_and_model_year() -> None:
    ends = quarter_end(_days("2026-01-15", "2026-06-30", "2026-07-01", "2026-12-31"))
    assert [str(to_date(d)) for d in ends] == [
        "2026-03-31",
        "2026-06-30",
        "2026-09-30",
        "2026-12-31",
    ]
    assert list(model_year(_days("2026-06-30", "2026-07-01"))) == [2026, 2027]


def test_dim_date_covers_whole_fiscal_years() -> None:
    table = build_dim_date(date(2023, 9, 1), date(2026, 10, 30)).to_pydict()
    assert table["calendar_date"][0] == date(2023, 4, 1)
    assert table["calendar_date"][-1] == date(2027, 3, 31)
    first_apr = table["calendar_date"].index(date(2025, 4, 1))
    assert table["fiscal_year_label"][first_apr] == "FY2026"
    assert table["fiscal_quarter"][first_apr] == 1
    assert table["day_name"][first_apr] == "Tuesday"


def test_history_passes_validation(dataset: Dataset) -> None:
    report = validate(dataset.root, dataset.contract)
    assert report.ok, report.render()
    assert report.row_counts["fact_production"] > 10_000


def test_history_ends_at_end_date(dataset: Dataset) -> None:
    con = connect_views(dataset.root, dataset.contract)
    end = dataset.generator.end_date
    assert con.execute("SELECT max(build_date) FROM fact_production").fetchone()[0] <= end
    assert con.execute("SELECT max(received_date) FROM fact_warranty_claim").fetchone()[0] <= end
    assert con.execute("SELECT max(opened_date) FROM dim_dealer").fetchone()[0] <= end


def test_messiness_is_present(dataset: Dataset) -> None:
    con = connect_views(dataset.root, dataset.contract)

    def one(sql: str) -> float:
        return con.execute(sql).fetchone()[0]

    assert one("SELECT count(*) FROM fact_sales WHERE is_cancelled") > 0
    assert one("SELECT count(*) FROM fact_warranty_claim WHERE claim_status = 'Rejected'") > 0
    assert one("SELECT count(*) FROM fact_warranty_claim WHERE claim_status = 'Pending'") > 0
    assert one("SELECT count(*) FROM fact_warranty_claim WHERE received_date - claim_date > 30") > 0
    assert one("SELECT count(*) FROM dim_dealer WHERE NOT is_active") > 0
    assert one("SELECT count(*) FROM fact_production WHERE exterior_colour IS NULL") > 0
    assert one("SELECT count(*) FROM fact_recall_vehicle WHERE remedy_completed_date IS NULL") > 0


def test_same_seed_gives_identical_files(dataset: Dataset, tmp_path: Path) -> None:
    generate_history(dataset.generator, dataset.reference, dataset.contract, tmp_path)
    for name in dataset.contract.tables:
        ours, theirs = table_files(tmp_path, name), table_files(dataset.root, name)
        assert [f.name for f in ours] == [f.name for f in theirs]
        for a, b in zip(ours, theirs, strict=True):
            assert a.read_bytes() == b.read_bytes(), f"{name}/{a.name} differs"


def test_different_seed_gives_different_data(dataset: Dataset, tmp_path: Path) -> None:
    other = dataset.generator.model_copy(update={"seed": dataset.generator.seed + 1})
    generate_history(other, dataset.reference, dataset.contract, tmp_path)
    query = "SELECT sum(net_price) FROM fact_sales"
    ours = connect_views(tmp_path, dataset.contract).execute(query).fetchone()
    theirs = connect_views(dataset.root, dataset.contract).execute(query).fetchone()
    assert ours != theirs


def test_refuses_to_overwrite_without_flag(dataset: Dataset) -> None:
    with pytest.raises(GenerationError, match="--overwrite"):
        generate_history(dataset.generator, dataset.reference, dataset.contract, dataset.root)


def test_ground_truth_records_dataset_and_patterns(dataset: Dataset) -> None:
    truth = yaml.safe_load((dataset.root / GROUND_TRUTH_FILE).read_text(encoding="utf-8"))
    assert truth["dataset"]["seed"] == dataset.generator.seed
    assert truth["dataset"]["preset"] == "test"
    assert len(truth["patterns"]) == 8
    for pattern in truth["patterns"].values():
        assert pattern["detection_sql"].lstrip().upper().startswith("WITH")


# --------------------------------------------------------------------------------------
# Daily increments
# --------------------------------------------------------------------------------------


@pytest.fixture
def history_copy(dataset: Dataset, tmp_path: Path) -> Path:
    import shutil

    root = tmp_path / "data"
    shutil.copytree(dataset.root, root)
    return root


def test_days_append_files_and_stay_valid(dataset: Dataset, history_copy: Path) -> None:
    for day in (1, 2, 3):
        result = generate_day(
            dataset.generator, dataset.reference, dataset.contract, history_copy, day
        )
        assert result.as_of == dataset.generator.end_date + timedelta(days=day)
    names = [f.name for f in table_files(history_copy, "fact_sales")]
    end = dataset.generator.end_date
    assert sorted(names) == sorted(
        ["fact_sales_history.parquet"]
        + [f"fact_sales_{end + timedelta(days=d)}.parquet" for d in (1, 2, 3)]
    )
    assert [f.name for f in table_files(history_copy, "dim_dealer")] == ["dim_dealer.parquet"]
    report = validate(history_copy, dataset.contract)
    assert report.ok, report.render()


def test_history_plus_days_equals_snapshot(
    dataset: Dataset, history_copy: Path, tmp_path: Path
) -> None:
    """History + days 1..3 holds exactly the fact rows of a snapshot taken at end_date + 3."""
    for day in (1, 2, 3):
        generate_day(dataset.generator, dataset.reference, dataset.contract, history_copy, day)
    snapshot = tmp_path / "snapshot"
    _, staging, _ = _simulate(dataset.generator, dataset.reference, snapshot)
    try:
        write_all(
            staging,
            dataset.contract,
            snapshot,
            dataset.generator.end_date + timedelta(days=3),
            "history",
            dataset.generator.active_preset.row_group_size,
            [],
        )
    finally:
        staging.cleanup()

    con = duckdb.connect()
    for table in dataset.contract.facts:
        pk = ", ".join(table.primary_key)

        def keys(root: Path, pk: str = pk, name: str = table.name) -> list[tuple]:
            glob = (root / name).as_posix() + "/**/*.parquet"
            return con.execute(f"SELECT {pk} FROM read_parquet('{glob}') ORDER BY {pk}").fetchall()

        assert keys(history_copy) == keys(snapshot), table.name


def test_day_requires_matching_history(dataset: Dataset, history_copy: Path) -> None:
    other = dataset.generator.model_copy(update={"seed": 7})
    with pytest.raises(GenerationError, match="seed"):
        generate_day(other, dataset.reference, dataset.contract, history_copy, 1)
    with pytest.raises(GenerationError, match="between 1 and"):
        generate_day(dataset.generator, dataset.reference, dataset.contract, history_copy, 0)


def test_day_requires_history(tmp_path: Path) -> None:
    contract, generator, reference = load_data_configs()
    with pytest.raises(GenerationError, match="No history"):
        generate_day(generator, reference, contract, tmp_path, 1)


def test_partitioned_layout(tmp_path: Path) -> None:
    contract, generator, reference = load_data_configs()
    preset = generator.active_preset.model_copy(update={"partitioned_tables": ["fact_sales"]})
    generator = generator.model_copy(update={"presets": {**generator.presets, "test": preset}})
    generate_history(generator, reference, contract, tmp_path)
    generate_day(generator, reference, contract, tmp_path, 1)
    files = [
        f.relative_to(tmp_path / "fact_sales").as_posix()
        for f in table_files(tmp_path, "fact_sales")
    ]
    assert "year=2026/month=08/history_0.parquet" in files
    assert "year=2026/month=09/day_2026-09-01_0.parquet" in files
    report = validate(tmp_path, contract)
    assert report.ok, report.render()
