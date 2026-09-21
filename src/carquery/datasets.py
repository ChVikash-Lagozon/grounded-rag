"""Dataset definitions from ``config/datasets.yaml``: where each contract table's files live.

Only paths and patterns are defined here; descriptions and columns stay in the schema
contract. Every contract table is a dataset, and naming a table the contract doesn't know is
a config error.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, field_validator

from carquery.config import ConfigError, load_yaml
from carquery.contract import SchemaContract

DATASETS_FILE = "datasets.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetDefaults(_Strict):
    path: str = "{table}"
    pattern: str = "**/*.parquet"


class DatasetOverride(_Strict):
    path: str | None = None
    pattern: str | None = None


class DatasetsFile(_Strict):
    defaults: DatasetDefaults = DatasetDefaults()
    tables: dict[str, DatasetOverride] = {}

    @field_validator("tables", mode="before")
    @classmethod
    def _none_is_empty(cls, value: object) -> object:
        return {} if value is None else value


@dataclass(frozen=True)
class Dataset:
    name: str
    path: str  # relative to the storage root, forward slashes
    pattern: str


def resolve_datasets(spec: DatasetsFile, contract: SchemaContract) -> list[Dataset]:
    """One dataset per contract table, in contract order."""
    unknown = sorted(set(spec.tables) - set(contract.tables))
    if unknown:
        raise ConfigError(f"{DATASETS_FILE}: tables not in the schema contract: {unknown}")
    datasets = []
    for name in contract.tables:
        override = spec.tables.get(name, DatasetOverride())
        path = (override.path or spec.defaults.path).format(table=name).strip("/")
        pattern = override.pattern or spec.defaults.pattern
        if not path or ".." in PurePosixPath(path).parts or PurePosixPath(path).is_absolute():
            raise ConfigError(f"{DATASETS_FILE}: {name}: path must be relative, got {path!r}")
        datasets.append(Dataset(name, path, pattern))
    return datasets


def load_datasets(
    contract: SchemaContract,
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Dataset]:
    return resolve_datasets(load_yaml(DATASETS_FILE, DatasetsFile, config_dir, env), contract)
