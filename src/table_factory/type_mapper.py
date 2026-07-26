"""Explicit, fail-closed Hive-to-Greenplum type mapping."""

from __future__ import annotations

import re

from table_factory.errors import TypeMappingError
from table_factory.models import MappedColumn, Table

_CHARACTER = re.compile(r"(VARCHAR|CHAR)\s*\(\s*(\d+)\s*\)\Z", re.IGNORECASE)
_DECIMAL = re.compile(
    r"(?:DECIMAL|NUMERIC)"
    r"(?:\s*\(\s*(\d+)(?:\s*,\s*(\d+))?\s*\))?\Z",
    re.IGNORECASE,
)
_DIRECT = {
    "STRING": "TEXT",
    "TINYINT": "SMALLINT",
    "SMALLINT": "SMALLINT",
    "INT": "INTEGER",
    "INTEGER": "INTEGER",
    "BIGINT": "BIGINT",
    "FLOAT": "REAL",
    "REAL": "REAL",
    "DOUBLE": "DOUBLE PRECISION",
    "DOUBLE PRECISION": "DOUBLE PRECISION",
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
}
_UNSUPPORTED_REASONS = {
    "ARRAY": "complex ARRAY values have no lossless scalar mapping",
    "MAP": "complex MAP values have no lossless scalar mapping",
    "STRUCT": "complex STRUCT values have no lossless scalar mapping",
    "UNIONTYPE": "UNIONTYPE values have no reliable Greenplum mapping",
    "VOID": "VOID is not a storable Greenplum column type",
    "BINARY": "BINARY transport semantics are not implemented end-to-end",
    "TIMESTAMPLOCALTZ": "timezone-specific timestamp semantics are not supported",
    "TIMESTAMP_LTZ": "timezone-specific timestamp semantics are not supported",
    "TIMESTAMP WITH LOCAL TIME ZONE": ("timezone-specific timestamp semantics are not supported"),
    "INTERVAL_DAY_TIME": "Hive interval semantics are not supported",
    "INTERVAL_YEAR_MONTH": "Hive interval semantics are not supported",
}


def _normalized(value: str) -> str:
    return " ".join(value.strip().upper().split())


def map_hive_type(data_type: str) -> str:
    """Map one supported Hive type or raise ``ValueError`` with a reason."""
    normalized = _normalized(data_type)
    direct = _DIRECT.get(normalized)
    if direct is not None:
        return direct

    character = _CHARACTER.fullmatch(normalized)
    if character is not None:
        return f"{character.group(1).upper()}({int(character.group(2))})"

    decimal = _DECIMAL.fullmatch(normalized)
    if decimal is not None:
        precision = decimal.group(1)
        scale = decimal.group(2)
        if precision is None:
            return "NUMERIC(10,0)"
        return f"NUMERIC({int(precision)},{int(scale or '0')})"

    leading_type = normalized.split("<", maxsplit=1)[0].split("(", maxsplit=1)[0]
    reason = _UNSUPPORTED_REASONS.get(
        normalized,
        _UNSUPPORTED_REASONS.get(
            leading_type,
            "no explicit, verified Greenplum mapping is configured",
        ),
    )
    raise ValueError(reason)


def map_table_columns(table: Table) -> tuple[MappedColumn, ...]:
    """Map all physical target columns with source context in every error."""
    mapped: list[MappedColumn] = []
    partition_names = {id(column) for column in table.partition_columns}
    for column in table.all_columns:
        try:
            greenplum_type = map_hive_type(column.data_type)
        except ValueError as error:
            raise TypeMappingError(
                f"cannot map source table {table.qualified_name}, "
                f"column {column.name}, Hive type {column.data_type}: {error}"
            ) from None
        mapped.append(
            MappedColumn(
                column=column,
                greenplum_type=greenplum_type,
                was_partition_column=id(column) in partition_names,
            )
        )
    return tuple(mapped)
