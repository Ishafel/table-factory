"""Application workflows shared by all command-line environments."""

from __future__ import annotations

import os
import unicodedata
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
from table_factory.models import Table, TablePlan
from table_factory.naming import build_target_names
from table_factory.parser import parse_hive_ddl
from table_factory.path_safety import has_untrusted_symlink_component
from table_factory.type_mapper import map_table_columns


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """Tables parsed from one input document."""

    path: Path
    label: str
    tables: tuple[Table, ...]


@dataclass(frozen=True, slots=True)
class PreparedWorkflow:
    """A fully validated workflow whose artifacts are safe to write."""

    parsed_files: tuple[ParsedFile, ...]
    plans: tuple[TablePlan, ...]
    artifacts: tuple[Artifact, ...]


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


def _reject_input_symlink_components(input_path: Path, *, label: str) -> None:
    if has_untrusted_symlink_component(input_path):
        raise TableFactoryError(f"input path must not contain a symbolic-link component: {label}")


def discover_sql_files(input_path: Path, *, cwd: Path) -> tuple[Path, ...]:
    """Find SQL inputs deterministically without depending on the host path."""
    label = display_path(input_path, cwd=cwd)
    if input_path.is_symlink() and has_untrusted_symlink_component(input_path):
        raise TableFactoryError(f"input path must not be a symbolic link: {label}")
    _reject_input_symlink_components(input_path, label=label)
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
            real_path = path.resolve(strict=True)
            sql = real_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            detail = getattr(error, "strerror", None) or "invalid UTF-8"
            raise TableFactoryError(f"cannot read input {label}: {detail}") from None
        try:
            tables = parse_hive_ddl(sql)
        except DdlParseError as error:
            raise DdlParseError(f"{label}: {error}") from None
        parsed.append(ParsedFile(path=real_path, label=label, tables=tables))
    return tuple(parsed)


def _comparison(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _ensure_unique_targets(plans: tuple[TablePlan, ...]) -> None:
    qualified_hive_sources: dict[tuple[str, str], str] = {}
    unqualified_hive_sources: dict[str, str] = {}
    for plan in plans:
        source = plan.source
        if source.database is None:
            unqualified_hive_sources.setdefault(
                _comparison(source.name),
                source.qualified_name,
            )
        else:
            qualified_hive_sources.setdefault(
                (_comparison(source.database), _comparison(source.name)),
                source.qualified_name,
            )

    hive_targets: dict[tuple[str, str], str] = {}
    greenplum_targets: dict[str, str] = {}
    for plan in plans:
        hive_name = plan.targets.hive_qualified_name
        hive_key = (
            _comparison(plan.targets.hive_database),
            _comparison(plan.targets.hive_table),
        )
        if hive_key in hive_targets:
            raise TableFactoryError(
                "multiple source tables map to the same Hive target: "
                f"{hive_targets[hive_key]} and {hive_name}"
            )
        hive_targets[hive_key] = hive_name

        colliding_source = qualified_hive_sources.get(hive_key)
        if colliding_source is None:
            colliding_source = unqualified_hive_sources.get(hive_key[1])
        if colliding_source is not None:
            raise TableFactoryError(
                f"generated Hive target {hive_name} would collide with "
                f"source Hive table {colliding_source} in the input batch"
            )

        for greenplum_name in (
            plan.targets.greenplum_external_qualified_name,
            plan.targets.greenplum_physical_qualified_name,
        ):
            greenplum_key = _comparison(greenplum_name)
            if greenplum_key in greenplum_targets:
                raise TableFactoryError(
                    "multiple generated tables map to the same Greenplum target: "
                    f"{greenplum_targets[greenplum_key]} and {greenplum_name}"
                )
            greenplum_targets[greenplum_key] = greenplum_name


def prepare(
    input_path: Path,
    *,
    config: FactoryConfig,
    cwd: Path,
) -> PreparedWorkflow:
    """Parse, semantically validate, render, and collision-check all inputs."""
    parsed_files = parse_files(input_path, cwd=cwd)

    planned: list[TablePlan] = []
    for parsed_file in parsed_files:
        for table in parsed_file.tables:
            planned.append(
                TablePlan(
                    source_label=parsed_file.label,
                    source=table,
                    targets=build_target_names(table, config),
                    mapped_columns=map_table_columns(table),
                )
            )
    plans = tuple(planned)
    _ensure_unique_targets(plans)

    rendered: list[Artifact] = []
    for plan in plans:
        rendered.extend(
            render_artifacts(
                plan,
                config=config,
                source_label=plan.source_label,
            )
        )
    ensure_unique_artifacts(rendered)
    return PreparedWorkflow(
        parsed_files=parsed_files,
        plans=plans,
        artifacts=tuple(rendered),
    )


def generate(
    input_path: Path,
    output_path: Path,
    *,
    config: FactoryConfig,
    cwd: Path,
) -> int:
    """Parse all inputs, then atomically write their generated artifacts."""
    prepared = prepare(input_path, config=config, cwd=cwd)
    write_artifacts(
        output_path,
        list(prepared.artifacts),
        input_paths=tuple(parsed_file.path for parsed_file in prepared.parsed_files),
    )
    return len(prepared.artifacts)
