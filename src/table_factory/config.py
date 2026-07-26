"""Configuration loading for table-factory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from table_factory.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class FactoryConfig:
    """Validated settings that influence deterministic rendering."""

    dialect: str = "hive"
    include_source_comment: bool = True
    filename_separator: str = "__"

    def as_dict(self) -> dict[str, object]:
        """Return the effective configuration without filesystem details."""
        return {
            "version": 1,
            "dialect": self.dialect,
            "output": {
                "include_source_comment": self.include_source_comment,
                "filename_separator": self.filename_separator,
            },
        }


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a YAML mapping")
    return value


def load_config(path: Path, *, display_name: str) -> FactoryConfig:
    """Read and validate a YAML configuration file.

    ``display_name`` is supplied by the CLI so an expected error never leaks an
    absolute host path into a report.
    """
    if not path.is_file():
        raise ConfigurationError(f"configuration file does not exist: {display_name}")

    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as error:
        detail = error.strerror or "I/O error"
        raise ConfigurationError(f"cannot read configuration {display_name}: {detail}") from None
    except UnicodeError:
        raise ConfigurationError(
            f"cannot read configuration {display_name}: invalid UTF-8"
        ) from None

    try:
        raw_value = yaml.safe_load(contents)
    except yaml.YAMLError as error:
        problem = getattr(error, "problem", None) or "invalid YAML"
        mark = getattr(error, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ConfigurationError(
            f"cannot read configuration {display_name}: {problem}{location}"
        ) from None

    raw = _mapping(raw_value, "configuration")
    version = raw.get("version")
    if type(version) is not int or version != 1:
        raise ConfigurationError("configuration version must be 1")

    dialect = raw.get("dialect", "hive")
    if dialect != "hive":
        raise ConfigurationError("only the 'hive' dialect is supported")

    output_value = raw.get("output", {})
    output = _mapping(output_value, "output")
    include_source_comment = output.get("include_source_comment", True)
    separator = output.get("filename_separator", "__")

    if not isinstance(include_source_comment, bool):
        raise ConfigurationError("output.include_source_comment must be a boolean")
    if not isinstance(separator, str) or not separator or len(separator) > 8:
        raise ConfigurationError(
            "output.filename_separator must be a non-empty string of at most 8 characters"
        )
    if any(character not in "._-" for character in separator):
        raise ConfigurationError("output.filename_separator may contain only '.', '_' and '-'")

    return FactoryConfig(
        dialect=dialect,
        include_source_comment=include_source_comment,
        filename_separator=separator,
    )
