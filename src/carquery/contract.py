"""Schema contract: typed model of ``config/schema_contract.yaml``.

The contract is the single source of truth for every table. It is used by the synthetic
generator, data validation, the data profile, the ER diagram and (later) the LLM context.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from carquery.config import load_yaml

CONTRACT_FILE = "schema_contract.yaml"

_DECIMAL = re.compile(r"DECIMAL\((\d+),\s*(\d+)\)")
_SIMPLE_TYPES: dict[str, pa.DataType] = {
    "VARCHAR": pa.string(),
    "BOOLEAN": pa.bool_(),
    "SMALLINT": pa.int16(),
    "INTEGER": pa.int32(),
    "BIGINT": pa.int64(),
    "DOUBLE": pa.float64(),
    "DATE": pa.date32(),
}


def arrow_type(sql_type: str) -> pa.DataType:
    """Map a contract (DuckDB) type name to a pyarrow type."""
    if sql_type in _SIMPLE_TYPES:
        return _SIMPLE_TYPES[sql_type]
    match = _DECIMAL.fullmatch(sql_type)
    if match:
        return pa.decimal128(int(match.group(1)), int(match.group(2)))
    raise ValueError(f"Unsupported contract type: {sql_type}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Column(_Strict):
    name: str
    type: str
    nullable: bool
    description: str
    allowed_values: list[str] | None = None
    min: float | None = None
    max: float | None = None
    unit: str | None = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        value = value.upper().replace(" ", "")
        arrow_type(value)
        return value

    @property
    def arrow_type(self) -> pa.DataType:
        return arrow_type(self.type)


class ForeignKey(_Strict):
    columns: list[str]
    references: str  # "table.column[,column]"

    @property
    def ref_table(self) -> str:
        return self.references.split(".", 1)[0]

    @property
    def ref_columns(self) -> list[str]:
        return self.references.split(".", 1)[1].split(",")


class Check(_Strict):
    name: str
    expr: str


class Table(_Strict):
    name: str = ""
    kind: Literal["dimension", "fact"]
    description: str
    grain: str
    date_column: str | None = None
    arrival_column: str | None = Field(
        default=None, description="Date the row reaches the data platform, if not date_column."
    )
    primary_key: list[str]
    columns: list[Column]
    foreign_keys: list[ForeignKey] = []
    checks: list[Check] = []

    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(f"{self.name}.{name}")

    def arrow_schema(self) -> pa.Schema:
        return pa.schema(
            [pa.field(c.name, c.arrow_type, nullable=c.nullable) for c in self.columns]
        )

    @model_validator(mode="after")
    def _columns_exist(self) -> Table:
        names = set(self.column_names)
        if len(names) != len(self.columns):
            raise ValueError("duplicate column names")
        referenced = [*self.primary_key, *(c for fk in self.foreign_keys for c in fk.columns)]
        referenced += [c for c in (self.date_column, self.arrival_column) if c]
        missing = [c for c in referenced if c not in names]
        if missing:
            raise ValueError(f"unknown columns referenced: {missing}")
        return self


class SchemaContract(_Strict):
    currency: str
    tables: dict[str, Table]

    @model_validator(mode="after")
    def _link(self) -> SchemaContract:
        for name, table in self.tables.items():
            table.name = name
        for table in self.tables.values():
            for fk in table.foreign_keys:
                target = self.tables.get(fk.ref_table)
                if target is None:
                    raise ValueError(f"{table.name}: FK references unknown table {fk.ref_table}")
                if fk.ref_columns != target.primary_key:
                    raise ValueError(
                        f"{table.name}: FK {fk.columns} must reference the primary key of "
                        f"{fk.ref_table} {target.primary_key}"
                    )
        return self

    def table(self, name: str) -> Table:
        return self.tables[name]

    @property
    def dimensions(self) -> list[Table]:
        return [t for t in self.tables.values() if t.kind == "dimension"]

    @property
    def facts(self) -> list[Table]:
        return [t for t in self.tables.values() if t.kind == "fact"]


def load_contract(
    config_dir: Path | None = None, env: Mapping[str, str] | None = None
) -> SchemaContract:
    """Load and validate ``config/schema_contract.yaml``."""
    return load_yaml(CONTRACT_FILE, SchemaContract, config_dir=config_dir, env=env)


# --------------------------------------------------------------------------------------
# ER diagram / schema documentation
# --------------------------------------------------------------------------------------


def mermaid_erd(contract: SchemaContract, full: bool = True) -> str:
    """Render the contract as a Mermaid ``erDiagram``.

    ``dim_date`` is a role-playing dimension joined to many date columns; its links are drawn
    once per referencing table. With ``full=False`` only key columns are listed.
    """
    lines = ["erDiagram"]
    for table in contract.tables.values():
        for fk in table.foreign_keys:
            target = contract.table(fk.ref_table)
            many = "|{" if _fk_is_mandatory_many(table, fk) else "o{"
            label = ", ".join(fk.columns)
            lines.append(f'    {target.name} ||--{many} {table.name} : "{label}"')
    for table in contract.tables.values():
        fk_columns = {c for fk in table.foreign_keys for c in fk.columns}
        lines.append(f"    {table.name} {{")
        for column in table.columns:
            keys = [
                k
                for k, on in (
                    ("PK", column.name in table.primary_key),
                    ("FK", column.name in fk_columns),
                )
                if on
            ]
            if not full and not keys:
                continue
            mermaid_type = column.type.split("(")[0].lower()
            suffix = f" {', '.join(keys)}" if keys else ""
            lines.append(f"        {mermaid_type} {column.name}{suffix}")
        lines.append("    }")
    return "\n".join(lines)


def _fk_is_mandatory_many(table: Table, fk: ForeignKey) -> bool:
    # Every vehicle has tracked components; everything else is optional on the "many" side.
    return table.name == "fact_vehicle_component" and fk.columns == ["vin"]


def schema_markdown(contract: SchemaContract) -> str:
    """Full schema documentation (ER diagram + column tables) for ``docs/schema.md``."""
    out = [
        "# Data schema",
        "",
        "<!-- Generated by `carq schema erd` from config/schema_contract.yaml. Do not edit. -->",
        "",
        f"Star schema for the car manufacturer PoC. All money is in {contract.currency}. "
        "`dim_date.calendar_date` is a role-playing date dimension: join it to any date column. "
        "`fact_production` is also the vehicle register (one row per `vin`).",
        "",
        "```mermaid",
        mermaid_erd(contract),
        "```",
        "",
    ]
    for table in contract.tables.values():
        out += [
            f"## `{table.name}` ({table.kind})",
            "",
            " ".join(table.description.split()),
            "",
            f"Grain: {table.grain}. Primary key: `{', '.join(table.primary_key)}`.",
            "",
            "| Column | Type | Null | Description |",
            "|---|---|---|---|",
        ]
        for column in table.columns:
            note = column.description
            if column.allowed_values:
                note += " Values: " + ", ".join(f"`{v}`" for v in column.allowed_values) + "."
            null = "yes" if column.nullable else "no"
            out.append(f"| `{column.name}` | {column.type} | {null} | {note} |")
        if table.foreign_keys:
            out += [
                "",
                "Foreign keys: "
                + "; ".join(
                    f"`{', '.join(fk.columns)}` → `{fk.references}`" for fk in table.foreign_keys
                )
                + ".",
            ]
        out.append("")
    return "\n".join(out)
