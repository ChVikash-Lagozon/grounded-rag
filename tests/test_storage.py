from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from carquery.config import ConfigError, load_config
from carquery.datasets import DatasetOverride, DatasetsFile, load_datasets, resolve_datasets
from carquery.storage import LocalConnector, get_connector

from .conftest import REPO_ROOT, Dataset


def test_repo_datasets_cover_the_contract(dataset: Dataset) -> None:
    datasets = load_datasets(dataset.contract, REPO_ROOT / "config", env={})
    assert [d.name for d in datasets] == list(dataset.contract.tables)
    assert all(d.path == d.name and d.pattern == "**/*.parquet" for d in datasets)


def test_dataset_override(dataset: Dataset) -> None:
    spec = DatasetsFile(tables={"fact_sales": DatasetOverride(path="sales/{table}/")})
    by_name = {d.name: d for d in resolve_datasets(spec, dataset.contract)}
    assert by_name["fact_sales"].path == "sales/fact_sales"
    assert by_name["dim_date"].path == "dim_date"


def test_unknown_dataset_is_a_config_error(dataset: Dataset) -> None:
    spec = DatasetsFile(tables={"fact_nope": DatasetOverride()})
    with pytest.raises(ConfigError, match="fact_nope"):
        resolve_datasets(spec, dataset.contract)


@pytest.mark.parametrize("path", ["../outside", "a/../../b"])
def test_dataset_path_must_stay_under_the_root(dataset: Dataset, path: str) -> None:
    spec = DatasetsFile(tables={"fact_sales": DatasetOverride(path=path)})
    with pytest.raises(ConfigError, match="relative"):
        resolve_datasets(spec, dataset.contract)


def test_local_connector_lists_files(dataset: Dataset) -> None:
    connector = LocalConnector(dataset.root)
    datasets = resolve_datasets(DatasetsFile(), dataset.contract)
    files = connector.list_files(next(d for d in datasets if d.name == "fact_sales"))
    assert files
    assert files == sorted(files, key=lambda f: f.uri)
    for info in files:
        path = Path(info.uri)
        assert path.is_absolute() and "\\" not in info.uri
        assert info.size == path.stat().st_size
        assert isinstance(info.modified, datetime) and info.modified.tzinfo is None


def test_local_connector_partitioned_layout_and_missing_dir(
    tmp_path: Path, dataset: Dataset
) -> None:
    datasets = {d.name: d for d in resolve_datasets(DatasetsFile(), dataset.contract)}
    for part in ("year=2026/month=1", "year=2026/month=2"):
        (tmp_path / "fact_sales" / part).mkdir(parents=True)
        (tmp_path / "fact_sales" / part / "data_0.parquet").write_bytes(b"x")
    (tmp_path / "fact_sales" / "notes.txt").write_text("ignored")
    connector = LocalConnector(tmp_path)

    files = connector.list_files(datasets["fact_sales"])
    assert [f.uri.split("fact_sales/")[1] for f in files] == [
        "year=2026/month=1/data_0.parquet",
        "year=2026/month=2/data_0.parquet",
    ]
    assert connector.list_files(datasets["dim_date"]) == []


def test_get_connector_from_config(config_dir: Path, tmp_path: Path) -> None:
    config = load_config(config_dir, env={"DATA_ROOT": os.fspath(tmp_path / "d")})
    connector = get_connector(config)
    assert connector.name == "local"
    assert connector.describe() == f"local:{(tmp_path / 'd').as_posix()}"


def test_unknown_backend_is_rejected(config_dir: Path) -> None:
    with pytest.raises(ConfigError, match=r"storage\.backend"):
        load_config(config_dir, env={"CARQ__STORAGE__BACKEND": "adls"})
