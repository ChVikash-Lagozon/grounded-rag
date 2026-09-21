from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

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
