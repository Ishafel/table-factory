"""Immutable source, target, and rendering-plan models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Column:
    """One column from a Hive CREATE TABLE statement."""

    name: str
    data_type: str
    comment: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        """Return a JSON-serializable representation."""
        return {
            "name": self.name,
            "type": self.data_type,
            "comment": self.comment,
        }


@dataclass(frozen=True, slots=True)
class Table:
    """A parsed, read-only source Hive table."""

    database: str | None
    name: str
    columns: tuple[Column, ...]
    partition_columns: tuple[Column, ...] = ()
    external: bool = False
    comment: str | None = None
    create_sql: str | None = None

    @property
    def qualified_name(self) -> str:
        """Return a human-readable qualified name without adding quoting."""
        if self.database:
            return f"{self.database}.{self.name}"
        return self.name

    @property
    def all_columns(self) -> tuple[Column, ...]:
        """Return physical target columns in their deterministic order."""
        return self.columns + self.partition_columns

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "database": self.database,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "external": self.external,
            "columns": [column.as_dict() for column in self.columns],
            "partition_columns": [column.as_dict() for column in self.partition_columns],
            "comment": self.comment,
        }


@dataclass(frozen=True, slots=True)
class TargetNames:
    """Validated target namespaces and table names for one source table."""

    hive_database: str
    hive_table: str
    greenplum_database: str
    greenplum_external_schema: str
    greenplum_external_table: str
    greenplum_physical_schema: str
    greenplum_physical_table: str

    @property
    def hive_qualified_name(self) -> str:
        return f"{self.hive_database}.{self.hive_table}"

    @property
    def greenplum_external_qualified_name(self) -> str:
        return f"{self.greenplum_external_schema}.{self.greenplum_external_table}"

    @property
    def greenplum_physical_qualified_name(self) -> str:
        return f"{self.greenplum_physical_schema}.{self.greenplum_physical_table}"

    def as_dict(self) -> dict[str, Any]:
        """Return target names without inventing a three-part GP SQL name."""
        return {
            "hive": {
                "database": self.hive_database,
                "table": self.hive_table,
                "qualified_name": self.hive_qualified_name,
            },
            "greenplum": {
                "database": self.greenplum_database,
                "external": {
                    "schema": self.greenplum_external_schema,
                    "table": self.greenplum_external_table,
                    "qualified_name": self.greenplum_external_qualified_name,
                },
                "physical": {
                    "schema": self.greenplum_physical_schema,
                    "table": self.greenplum_physical_table,
                    "qualified_name": self.greenplum_physical_qualified_name,
                },
            },
        }


@dataclass(frozen=True, slots=True)
class MappedColumn:
    """One Hive column and its explicit Greenplum type."""

    column: Column
    greenplum_type: str
    was_partition_column: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.column.as_dict(),
            "greenplum_type": self.greenplum_type,
            "was_partition_column": self.was_partition_column,
        }


@dataclass(frozen=True, slots=True)
class TablePlan:
    """A fully validated source-to-target plan, ready for rendering."""

    source_label: str
    source: Table
    targets: TargetNames
    mapped_columns: tuple[MappedColumn, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_label,
            **self.source.as_dict(),
            "targets": self.targets.as_dict(),
            "greenplum_columns": [mapped_column.as_dict() for mapped_column in self.mapped_columns],
        }
