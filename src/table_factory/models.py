"""Small immutable models used by the parser and renderer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Column:
    """One column from a Hive CREATE TABLE statement."""

    name: str
    data_type: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation."""
        return {"name": self.name, "type": self.data_type}


@dataclass(frozen=True, slots=True)
class Table:
    """The subset of a Hive table definition needed by the generators."""

    database: str | None
    name: str
    columns: tuple[Column, ...]
    create_sql: str | None = None

    @property
    def qualified_name(self) -> str:
        """Return a human-readable qualified name without adding quoting."""
        if self.database:
            return f"{self.database}.{self.name}"
        return self.name

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "database": self.database,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "columns": [column.as_dict() for column in self.columns],
        }
