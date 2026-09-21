from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from carquery.config import ConfigError
from carquery.contract import arrow_type, load_contract, mermaid_erd, schema_markdown

from .conftest import REPO_ROOT


def test_repo_contract_loads(repo_config_dir: Path) -> None:
    contract = load_contract(repo_config_dir, env={})
    assert contract.currency == "EUR"
    assert len(contract.dimensions) == 8
    assert len(contract.facts) == 7
    assert contract.table("dim_date").primary_key == ["calendar_date"]
    assert contract.table("fact_recall_vehicle").primary_key == ["recall_id", "vin"]


def test_every_foreign_key_targets_a_primary_key(repo_config_dir: Path) -> None:
    contract = load_contract(repo_config_dir, env={})
    for table in contract.tables.values():
        for fk in table.foreign_keys:
            assert fk.ref_columns == contract.table(fk.ref_table).primary_key


def test_arrow_schema_matches_contract(repo_config_dir: Path) -> None:
    sales = load_contract(repo_config_dir, env={}).table("fact_sales").arrow_schema()
    assert sales.field("sale_id").type == pa.int64()
    assert sales.field("sale_date").type == pa.date32()
    assert sales.field("net_price").type == pa.decimal128(12, 2)
    assert not sales.field("vin").nullable


@pytest.mark.parametrize(
    ("sql_type", "expected"),
    [("VARCHAR", pa.string()), ("SMALLINT", pa.int16()), ("DECIMAL(6,4)", pa.decimal128(6, 4))],
)
def test_arrow_type(sql_type: str, expected: pa.DataType) -> None:
    assert arrow_type(sql_type) == expected


def _write_contract(config_dir: Path, body: str) -> None:
    (config_dir / "schema_contract.yaml").write_text(body, encoding="utf-8")


MINIMAL = """\
currency: EUR
tables:
  dim_a:
    kind: dimension
    description: A.
    grain: one row per a
    primary_key: [a_id]
    columns:
      - {name: a_id, type: INTEGER, nullable: false, description: Key.}
  fact_b:
    kind: fact
    description: B.
    grain: one row per b
    primary_key: [b_id]
    columns:
      - {name: b_id, type: BIGINT, nullable: false, description: Key.}
      - {name: a_id, type: INTEGER, nullable: false, description: FK.}
    foreign_keys:
      - {columns: [a_id], references: %s}
"""


def test_unknown_foreign_key_table_is_rejected(config_dir: Path) -> None:
    _write_contract(config_dir, MINIMAL % "dim_missing.a_id")
    with pytest.raises(ConfigError, match="unknown table dim_missing"):
        load_contract(config_dir, env={})


def test_foreign_key_must_reference_primary_key(config_dir: Path) -> None:
    _write_contract(config_dir, MINIMAL % "fact_b.a_id")
    with pytest.raises(ConfigError, match="primary key"):
        load_contract(config_dir, env={})


def test_unknown_type_is_rejected(config_dir: Path) -> None:
    _write_contract(config_dir, (MINIMAL % "dim_a.a_id").replace("BIGINT", "UUID"))
    with pytest.raises(ConfigError, match="Unsupported contract type"):
        load_contract(config_dir, env={})


def test_mermaid_erd_lists_every_table_and_relationship(repo_config_dir: Path) -> None:
    contract = load_contract(repo_config_dir, env={})
    erd = mermaid_erd(contract)
    assert erd.startswith("erDiagram")
    for name in contract.tables:
        assert f"    {name} {{" in erd
    assert 'dim_dealer ||--o{ fact_sales : "dealer_id"' in erd
    assert "varchar vin PK, FK" in erd  # fact_vehicle_component


def test_committed_schema_doc_is_up_to_date(repo_config_dir: Path) -> None:
    committed = (REPO_ROOT / "docs" / "schema.md").read_text(encoding="utf-8")
    assert committed == schema_markdown(load_contract(repo_config_dir, env={})), (
        "docs/schema.md is stale: run `uv run carq schema erd`"
    )
