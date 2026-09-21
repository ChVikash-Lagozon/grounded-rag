"""Storage connectors: list a dataset's files and prepare DuckDB to read them.

The catalog and refresh only talk to :class:`StorageConnector`, so the data location is a
config choice (``storage.backend``). Only ``local`` exists today; a remote backend (ADLS, S3)
is one new class plus an entry in :data:`CONNECTORS`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import duckdb

from carquery.config import AppConfig
from carquery.datasets import Dataset


@dataclass(frozen=True)
class FileInfo:
    uri: str  # what DuckDB reads: an absolute local path (forward slashes) or a remote URL
    size: int
    modified: datetime  # UTC, naive


class StorageConnector(Protocol):
    name: str

    def configure(self, con: duckdb.DuckDBPyConnection) -> None:
        """Prepare a connection to read this storage (extensions, per-connection secrets)."""
        ...

    def list_files(self, dataset: Dataset) -> list[FileInfo]:
        """The dataset's files, sorted by URI."""
        ...

    def describe(self) -> str:
        """A one-line description for logs and the UI. Never includes secrets."""
        ...


class LocalConnector:
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def configure(self, con: duckdb.DuckDBPyConnection) -> None:
        return None

    def list_files(self, dataset: Dataset) -> list[FileInfo]:
        directory = self.root / dataset.path
        if not directory.is_dir():
            return []
        files = []
        for path in directory.glob(dataset.pattern):
            if not path.is_file():
                continue
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, UTC).replace(tzinfo=None)
            files.append(FileInfo(path.resolve().as_posix(), stat.st_size, modified))
        return sorted(files, key=lambda f: f.uri)

    def describe(self) -> str:
        return f"local:{self.root.as_posix()}"


CONNECTORS: dict[str, Callable[[AppConfig], StorageConnector]] = {
    "local": lambda config: LocalConnector(config.paths.data_root),
}


def get_connector(config: AppConfig) -> StorageConnector:
    return CONNECTORS[config.storage.backend](config)
