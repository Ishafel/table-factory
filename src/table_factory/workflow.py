"""Application workflows shared by all command-line environments."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from table_factory.config import FactoryConfig
from table_factory.errors import DdlParseError, TableFactoryError
from table_factory.generator import (
    Artifact,
    ensure_unique_artifacts,
    render_artifacts,
    write_artifacts,
)
from table_factory.models import Table
from table_factory.parser import parse_hive_ddl


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """Tables parsed from one input document."""

    label: str
    tables: tuple[Table, ...]


def display_path(path: Path, *, cwd: Path) -> str:
    """Return a stable path label that never exposes an absolute host prefix."""
    try:
        label = path.resolve().relative_to(cwd.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        label = path.name or "."
    return "".join(
        character
        if character.isprintable() and character not in "\r\n"
        else f"\\u{ord(character):04x}"
        for character in label
    )


def resolve_from_cwd(value: str, *, cwd: Path) -> Path:
    """Resolve a CLI path using the current working directory as its base."""
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate


def discover_sql_files(input_path: Path, *, cwd: Path) -> tuple[Path, ...]:
    """Find SQL inputs deterministically without depending on the host path."""
    label = display_path(input_path, cwd=cwd)
    if input_path.is_symlink():
        raise TableFactoryError(f"input path must not be a symbolic link: {label}")
    if input_path.is_file():
        if input_path.suffix.lower() != ".sql":
            raise TableFactoryError(f"input file is not SQL: {label}")
        return (input_path,)
    if not input_path.is_dir():
        raise TableFactoryError(f"input path does not exist: {label}")

    def raise_scan_error(error: OSError) -> None:
        error_path = (
            Path(os.fsdecode(error.filename))
            if isinstance(error.filename, (str, bytes))
            else input_path
        )
        error_label = display_path(error_path, cwd=cwd)
        detail = error.strerror or "I/O error"
        raise TableFactoryError(f"cannot scan input {error_label}: {detail}") from None

    discovered: list[Path] = []
    for directory, directory_names, filenames in os.walk(
        input_path,
        onerror=raise_scan_error,
    ):
        directory_names.sort()
        for directory_name in directory_names:
            path = Path(directory) / directory_name
            if path.is_symlink():
                path_label = display_path(path, cwd=cwd)
                raise TableFactoryError(
                    f"input directory must not contain a symbolic link: {path_label}"
                )
        filenames.sort()
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() != ".sql":
                continue
            if path.is_symlink():
                path_label = display_path(path, cwd=cwd)
                raise TableFactoryError(f"input SQL file must not be a symbolic link: {path_label}")
            if path.is_file():
                discovered.append(path)

    files = tuple(
        sorted(
            discovered,
            key=lambda path: path.relative_to(input_path).as_posix(),
        )
    )
    if not files:
        raise TableFactoryError(f"no SQL files found in input: {label}")
    return files


def parse_files(input_path: Path, *, cwd: Path) -> tuple[ParsedFile, ...]:
    """Read and parse every input before any output is created."""
    parsed: list[ParsedFile] = []
    for path in discover_sql_files(input_path, cwd=cwd):
        label = display_path(path, cwd=cwd)
        try:
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            detail = getattr(error, "strerror", None) or "invalid UTF-8"
            raise TableFactoryError(f"cannot read input {label}: {detail}") from None
        try:
            tables = parse_hive_ddl(sql)
        except DdlParseError as error:
            raise DdlParseError(f"{label}: {error}") from None
        parsed.append(ParsedFile(label=label, tables=tables))
    return tuple(parsed)


def generate(
    input_path: Path,
    output_path: Path,
    *,
    config: FactoryConfig,
    cwd: Path,
) -> int:
    """Parse all inputs, then atomically write their generated artifacts."""
    parsed_files = parse_files(input_path, cwd=cwd)
    artifacts: list[Artifact] = []
    for parsed_file in parsed_files:
        for table in parsed_file.tables:
            artifacts.extend(
                render_artifacts(
                    table,
                    config=config,
                    source_label=Path(parsed_file.label).name,
                )
            )
    ensure_unique_artifacts(artifacts)
    write_artifacts(output_path, artifacts)
    return len(artifacts)
