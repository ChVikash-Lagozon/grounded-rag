from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import structlog

from carquery.config import AppConfig, load_config
from carquery.contract import SchemaContract, load_contract
from carquery.datasets import Dataset as DatasetDef
from carquery.datasets import DatasetsFile, resolve_datasets
from carquery.generator import generate_history, load_generator_config, load_reference
from carquery.generator.config import GeneratorConfig, ReferenceData

REPO_ROOT = Path(__file__).resolve().parents[1]

APP_YAML = """\
environment: ${CARQ_ENV:-dev}
paths:
  data_root: ${DATA_ROOT:-data}
  catalog_path: data/catalog.duckdb
logging:
  level: INFO
  format: console
"""


@pytest.fixture
def repo_config_dir() -> Path:
    return REPO_ROOT / "config"


# --------------------------------------------------------------------------------------
# Generated data (the `test` preset, generated once per session)
# --------------------------------------------------------------------------------------

TEST_PRESET = "test"
DATA_CONFIG_FILES = (
    "schema_contract.yaml",
    "generator.yaml",
    "synthetic_reference.yaml",
    "datasets.yaml",
)


@dataclass
class Dataset:
    root: Path
    contract: SchemaContract
    generator: GeneratorConfig
    reference: ReferenceData


def load_data_configs(
    env: dict[str, str] | None = None,
) -> tuple[SchemaContract, GeneratorConfig, ReferenceData]:
    config_dir, env = REPO_ROOT / "config", env or {}
    return (
        load_contract(config_dir, env=env),
        load_generator_config(config_dir, env=env, preset=TEST_PRESET),
        load_reference(config_dir, env=env),
    )


@pytest.fixture(scope="session")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    """History for the `test` preset, generated once and shared (treat as read-only)."""
    contract, generator, reference = load_data_configs()
    root = tmp_path_factory.mktemp("dataset")
    generate_history(generator, reference, contract, root)
    return Dataset(root, contract, generator, reference)


@dataclass
class CatalogSetup:
    """A private copy of the `test` dataset plus a config whose catalog lives next to it."""

    config: AppConfig
    data: Dataset  # root = the copy (safe to modify)
    datasets: list[DatasetDef]


@pytest.fixture
def catalog_setup(dataset: Dataset, config_dir: Path) -> CatalogSetup:
    root = config_dir.parent / "data"
    shutil.copytree(dataset.root, root)
    config = load_config(config_dir, env={"DATA_ROOT": str(root)})
    copy = Dataset(root, dataset.contract, dataset.generator, dataset.reference)
    return CatalogSetup(config, copy, resolve_datasets(DatasetsFile(), dataset.contract))


@pytest.fixture
def data_config_dir(config_dir: Path) -> Path:
    """The temporary config dir plus copies of the repo's data config files."""
    for name in DATA_CONFIG_FILES:
        shutil.copy(REPO_ROOT / "config" / name, config_dir / name)
    return config_dir


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A throwaway config directory with a minimal app.yaml; its parent is the 'repo root'."""
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "app.yaml").write_text(APP_YAML, encoding="utf-8")
    return directory


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_carquery_handler", False)]:
        root.removeHandler(handler)
        handler.close()
