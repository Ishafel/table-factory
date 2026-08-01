"""Application workflows shared by all command-line environments."""

from __future__ import annotations

import os
import stat
import unicodedata
from contextlib import suppress
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
from table_factory.path_safety import (
    FileIdentity,
    PathIdentityChangedError,
    SecurePathUnsupportedError,
    UntrustedSymlinkError,
    open_pinned_path,
    open_verified_entry,
)
from table_factory.type_mapper import map_table_columns


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """Tables parsed from one input document."""

    path: Path
    label: str
    tables: tuple[Table, ...]
    identity: FileIdentity


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


@dataclass(frozen=True, slots=True)
class _ScannedSql:
    path: Path
    label: str
    sql: str | None
    identity: FileIdentity


def _open_input_root(
    input_path: Path,
    *,
    cwd: Path,
) -> tuple[int, os.stat_result, Path, str]:
    label = display_path(input_path, cwd=cwd)
    try:
        descriptor, status = open_pinned_path(input_path, create_directory=False)
    except UntrustedSymlinkError as error:
        if error.final_component:
            raise TableFactoryError(f"input path must not be a symbolic link: {label}") from None
        raise TableFactoryError(
            f"input path must not contain a symbolic-link component: {label}"
        ) from None
    except FileNotFoundError:
        raise TableFactoryError(f"input path does not exist: {label}") from None
    except PathIdentityChangedError:
        raise TableFactoryError(f"input path changed while it was being opened: {label}") from None
    except SecurePathUnsupportedError as error:
        raise TableFactoryError(f"cannot safely open input {label}: {error}") from None
    except OSError as error:
        detail = error.strerror or "I/O error"
        raise TableFactoryError(f"cannot open input {label}: {detail}") from None
    return descriptor, status, Path(os.path.abspath(input_path)), label


def _read_utf8(descriptor: int, *, label: str) -> str:
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeError) as error:
        with suppress(OSError):
            os.close(descriptor)
        detail = getattr(error, "strerror", None) or "invalid UTF-8"
        raise TableFactoryError(f"cannot read input {label}: {detail}") from None


def _opened_sql(
    descriptor: int,
    status: os.stat_result,
    *,
    path: Path,
    label: str,
    read_contents: bool,
) -> _ScannedSql:
    identity = FileIdentity.from_stat(status)
    if read_contents:
        sql = _read_utf8(descriptor, label=label)
    else:
        os.close(descriptor)
        sql = None
    return _ScannedSql(path=path, label=label, sql=sql, identity=identity)


def _scan_directory(
    directory_fd: int,
    *,
    root_path: Path,
    relative_directory: Path,
    cwd: Path,
    read_contents: bool,
    discovered: list[_ScannedSql],
) -> None:
    directory_path = root_path / relative_directory
    try:
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as error:
        directory_label = display_path(directory_path, cwd=cwd)
        detail = error.strerror or "I/O error"
        raise TableFactoryError(f"cannot scan input {directory_label}: {detail}") from None

    for name in names:
        relative_path = relative_directory / name
        path = root_path / relative_path
        label = display_path(path, cwd=cwd)
        try:
            checked_status = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            detail = error.strerror or "I/O error"
            raise TableFactoryError(f"cannot scan input {label}: {detail}") from None

        if stat.S_ISLNK(checked_status.st_mode):
            try:
                target_status = os.stat(name, dir_fd=directory_fd, follow_symlinks=True)
            except OSError:
                target_status = None
            if target_status is not None and stat.S_ISDIR(target_status.st_mode):
                raise TableFactoryError(
                    f"input directory must not contain a symbolic link: {label}"
                )
            if path.suffix.lower() == ".sql":
                raise TableFactoryError(f"input SQL file must not be a symbolic link: {label}")
            continue

        if stat.S_ISDIR(checked_status.st_mode):
            try:
                child_fd, _child_status = open_verified_entry(
                    directory_fd,
                    name,
                    checked_status,
                    expected="directory",
                )
            except (OSError, PathIdentityChangedError, UntrustedSymlinkError) as error:
                detail = getattr(error, "strerror", None) or "path changed during scan"
                raise TableFactoryError(f"cannot scan input {label}: {detail}") from None
            try:
                _scan_directory(
                    child_fd,
                    root_path=root_path,
                    relative_directory=relative_path,
                    cwd=cwd,
                    read_contents=read_contents,
                    discovered=discovered,
                )
            finally:
                os.close(child_fd)
            continue

        if not stat.S_ISREG(checked_status.st_mode) or path.suffix.lower() != ".sql":
            continue
        try:
            file_fd, opened_status = open_verified_entry(
                directory_fd,
                name,
                checked_status,
                expected="regular",
            )
        except (OSError, PathIdentityChangedError, UntrustedSymlinkError) as error:
            detail = getattr(error, "strerror", None) or "path changed while it was being opened"
            raise TableFactoryError(f"cannot read input {label}: {detail}") from None
        discovered.append(
            _opened_sql(
                file_fd,
                opened_status,
                path=path,
                label=label,
                read_contents=read_contents,
            )
        )


def _scan_sql_files(
    input_path: Path,
    *,
    cwd: Path,
    read_contents: bool,
) -> tuple[_ScannedSql, ...]:
    root_fd, root_status, root_path, root_label = _open_input_root(input_path, cwd=cwd)
    if stat.S_ISREG(root_status.st_mode):
        if root_path.suffix.lower() != ".sql":
            os.close(root_fd)
            raise TableFactoryError(f"input file is not SQL: {root_label}")
        return (
            _opened_sql(
                root_fd,
                root_status,
                path=root_path,
                label=root_label,
                read_contents=read_contents,
            ),
        )
    if not stat.S_ISDIR(root_status.st_mode):
        os.close(root_fd)
        raise TableFactoryError(f"input path does not exist: {root_label}")

    discovered: list[_ScannedSql] = []
    try:
        _scan_directory(
            root_fd,
            root_path=root_path,
            relative_directory=Path(),
            cwd=cwd,
            read_contents=read_contents,
            discovered=discovered,
        )
    finally:
        os.close(root_fd)
    discovered.sort(key=lambda item: item.path.relative_to(root_path).as_posix())
    if not discovered:
        raise TableFactoryError(f"no SQL files found in input: {root_label}")
    return tuple(discovered)


def discover_sql_files(input_path: Path, *, cwd: Path) -> tuple[Path, ...]:
    """Find SQL inputs deterministically without depending on the host path."""
    return tuple(
        scanned.path for scanned in _scan_sql_files(input_path, cwd=cwd, read_contents=False)
    )


def parse_files(input_path: Path, *, cwd: Path) -> tuple[ParsedFile, ...]:
    """Read and parse every input before any output is created."""
    parsed: list[ParsedFile] = []
    for scanned in _scan_sql_files(input_path, cwd=cwd, read_contents=True):
        if scanned.sql is None:
            raise AssertionError("input scanner omitted requested SQL contents")
        try:
            tables = parse_hive_ddl(scanned.sql)
        except DdlParseError as error:
            raise DdlParseError(f"{scanned.label}: {error}") from None
        parsed.append(
            ParsedFile(
                path=scanned.path,
                label=scanned.label,
                tables=tables,
                identity=scanned.identity,
            )
        )
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
        input_identities=tuple(parsed_file.identity for parsed_file in prepared.parsed_files),
    )
    return len(prepared.artifacts)
