"""Application workflows shared by all command-line environments."""

from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Generator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from table_factory.config import FactoryConfig
from table_factory.errors import DdlParseError, TableFactoryError
from table_factory.generator import (
    Artifact,
    artifact_filenames,
    ensure_unique_artifact_names,
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

_MAX_INPUT_DIRECTORY_DEPTH = 64
_MAX_INPUT_SQL_FILES = 1024
_MAX_INPUT_SQL_FILE_BYTES = 8 * 1024 * 1024
_MAX_INPUT_SQL_BYTES = 64 * 1024 * 1024
_MAX_INPUT_DIRECTORIES = 4096
_MAX_INPUT_ENTRIES = 16_384


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
    byte_count: int


@dataclass(frozen=True, slots=True)
class _PendingSql:
    path: Path
    relative_path: Path
    label: str
    identity: FileIdentity


@dataclass(frozen=True, slots=True)
class _PendingDirectory:
    relative_path: Path
    label: str
    identity: FileIdentity
    depth: int


@dataclass(slots=True)
class _ScanBudget:
    root_label: str
    directory_count: int = 1
    entry_count: int = 0
    sql_file_count: int = 0
    sql_byte_count: int = 0


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


def _input_file_limit_error(label: str) -> TableFactoryError:
    return TableFactoryError(
        f"input SQL file exceeds the supported limit of {_MAX_INPUT_SQL_FILE_BYTES} bytes: {label}"
    )


def _input_aggregate_limit_error(root_label: str) -> TableFactoryError:
    return TableFactoryError(
        f"input SQL files exceed the supported aggregate limit of "
        f"{_MAX_INPUT_SQL_BYTES} bytes: {root_label}"
    )


def _read_utf8(
    descriptor: int,
    *,
    label: str,
    byte_limit: int,
    overflow_error: TableFactoryError,
) -> tuple[str, int]:
    try:
        with os.fdopen(descriptor, "rb") as handle:
            encoded = handle.read(byte_limit + 1)
    except MemoryError:
        with suppress(OSError):
            os.close(descriptor)
        raise TableFactoryError(f"cannot read input {label}: insufficient memory") from None
    except (OSError, UnicodeError) as error:
        with suppress(OSError):
            os.close(descriptor)
        detail = getattr(error, "strerror", None) or "I/O error"
        raise TableFactoryError(f"cannot read input {label}: {detail}") from None

    if len(encoded) > byte_limit:
        raise overflow_error
    try:
        return encoded.decode("utf-8"), len(encoded)
    except UnicodeError:
        raise TableFactoryError(f"cannot read input {label}: invalid UTF-8") from None
    except MemoryError:
        raise TableFactoryError(f"cannot read input {label}: insufficient memory") from None


def _opened_sql(
    descriptor: int,
    status: os.stat_result,
    *,
    path: Path,
    label: str,
    read_contents: bool,
    byte_limit: int,
    overflow_error: TableFactoryError,
) -> _ScannedSql:
    identity = FileIdentity.from_stat(status)
    if read_contents:
        sql, byte_count = _read_utf8(
            descriptor,
            label=label,
            byte_limit=byte_limit,
            overflow_error=overflow_error,
        )
    else:
        os.close(descriptor)
        sql = None
        byte_count = status.st_size
    return _ScannedSql(
        path=path,
        label=label,
        sql=sql,
        identity=identity,
        byte_count=byte_count,
    )


def _bounded_directory_names(
    directory_fd: int,
    *,
    directory_label: str,
    budget: _ScanBudget,
) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                budget.entry_count += 1
                if budget.entry_count > _MAX_INPUT_ENTRIES:
                    raise TableFactoryError(
                        f"input contains more than {_MAX_INPUT_ENTRIES} filesystem entries: "
                        f"{budget.root_label}"
                    )
                names.append(entry.name)
    except OSError as error:
        detail = error.strerror or "I/O error"
        raise TableFactoryError(f"cannot scan input {directory_label}: {detail}") from None
    names.sort()
    return names


def _reopen_scanned_entry(
    root_fd: int,
    pending_path: Path,
    expected_identity: FileIdentity,
    *,
    expected: Literal["directory", "regular"],
    label: str,
) -> tuple[int, os.stat_result]:
    try:
        current_fd = os.dup(root_fd)
    except OSError as error:
        detail = error.strerror or "I/O error"
        action = "scan" if expected == "directory" else "read"
        raise TableFactoryError(f"cannot {action} input {label}: {detail}") from None

    try:
        components = pending_path.parts
        if not components:
            raise ValueError("a scanned descendant path must not be empty")
        opened_status = os.fstat(current_fd)
        for index, component in enumerate(components):
            checked_status = os.stat(
                component,
                dir_fd=current_fd,
                follow_symlinks=False,
            )
            final_component = index == len(components) - 1
            if final_component and FileIdentity.from_stat(checked_status) != expected_identity:
                raise PathIdentityChangedError("path entry changed after input discovery")
            child_fd, opened_status = open_verified_entry(
                current_fd,
                component,
                checked_status,
                expected=expected if final_component else "directory",
                final_component=final_component,
            )
            os.close(current_fd)
            current_fd = child_fd
        if FileIdentity.from_stat(opened_status) != expected_identity:
            raise PathIdentityChangedError("path entry changed after input discovery")
        return current_fd, opened_status
    except (OSError, ValueError, PathIdentityChangedError, UntrustedSymlinkError) as error:
        with suppress(OSError):
            os.close(current_fd)
        error_detail = getattr(error, "strerror", None)
        if not isinstance(error_detail, str) or not error_detail:
            error_detail = (
                "path changed during scan"
                if expected == "directory"
                else "path changed while it was being opened"
            )
        action = "scan" if expected == "directory" else "read"
        raise TableFactoryError(f"cannot {action} input {label}: {error_detail}") from None


def _register_sql_file(
    *,
    path: Path,
    relative_path: Path,
    label: str,
    status: os.stat_result,
    budget: _ScanBudget,
) -> _PendingSql:
    budget.sql_file_count += 1
    if budget.sql_file_count > _MAX_INPUT_SQL_FILES:
        raise TableFactoryError(
            f"input contains more than {_MAX_INPUT_SQL_FILES} SQL files: {budget.root_label}"
        )

    byte_count = status.st_size
    if byte_count > _MAX_INPUT_SQL_FILE_BYTES:
        raise _input_file_limit_error(label)
    budget.sql_byte_count += byte_count
    if budget.sql_byte_count > _MAX_INPUT_SQL_BYTES:
        raise _input_aggregate_limit_error(budget.root_label)
    return _PendingSql(
        path=path,
        relative_path=relative_path,
        label=label,
        identity=FileIdentity.from_stat(status),
    )


def _collect_sql_metadata(
    root_fd: int,
    root_status: os.stat_result,
    *,
    root_path: Path,
    root_label: str,
    cwd: Path,
) -> tuple[_PendingSql, ...]:
    budget = _ScanBudget(root_label=root_label)
    pending_directories = [
        _PendingDirectory(
            relative_path=Path(),
            label=root_label,
            identity=FileIdentity.from_stat(root_status),
            depth=0,
        )
    ]
    discovered: list[_PendingSql] = []

    while pending_directories:
        pending_directory = pending_directories.pop()
        close_directory = bool(pending_directory.relative_path.parts)
        if close_directory:
            directory_fd, _directory_status = _reopen_scanned_entry(
                root_fd,
                pending_directory.relative_path,
                pending_directory.identity,
                expected="directory",
                label=pending_directory.label,
            )
        else:
            directory_fd = root_fd

        try:
            names = _bounded_directory_names(
                directory_fd,
                directory_label=pending_directory.label,
                budget=budget,
            )
            child_directories: list[_PendingDirectory] = []
            for name in names:
                relative_path = pending_directory.relative_path / name
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
                        raise TableFactoryError(
                            f"input SQL file must not be a symbolic link: {label}"
                        )
                    continue

                if stat.S_ISDIR(checked_status.st_mode):
                    child_depth = pending_directory.depth + 1
                    if child_depth > _MAX_INPUT_DIRECTORY_DEPTH:
                        raise TableFactoryError(
                            "input directory nesting exceeds the supported limit of "
                            f"{_MAX_INPUT_DIRECTORY_DEPTH}: {label}"
                        )
                    budget.directory_count += 1
                    if budget.directory_count > _MAX_INPUT_DIRECTORIES:
                        raise TableFactoryError(
                            f"input contains more than {_MAX_INPUT_DIRECTORIES} directories: "
                            f"{root_label}"
                        )
                    child_directories.append(
                        _PendingDirectory(
                            relative_path=relative_path,
                            label=label,
                            identity=FileIdentity.from_stat(checked_status),
                            depth=child_depth,
                        )
                    )
                    continue

                if not stat.S_ISREG(checked_status.st_mode) or path.suffix.lower() != ".sql":
                    continue
                discovered.append(
                    _register_sql_file(
                        path=path,
                        relative_path=relative_path,
                        label=label,
                        status=checked_status,
                        budget=budget,
                    )
                )
            pending_directories.extend(reversed(child_directories))
        finally:
            if close_directory:
                os.close(directory_fd)

    discovered.sort(key=lambda item: item.relative_path.as_posix())
    return tuple(discovered)


def _scan_sql_files(
    input_path: Path,
    *,
    cwd: Path,
    read_contents: bool,
) -> Generator[_ScannedSql, None, None]:
    root_fd, root_status, root_path, root_label = _open_input_root(input_path, cwd=cwd)
    try:
        if stat.S_ISREG(root_status.st_mode):
            if root_path.suffix.lower() != ".sql":
                raise TableFactoryError(f"input file is not SQL: {root_label}")
            direct_budget = _ScanBudget(root_label=root_label, directory_count=0)
            _register_sql_file(
                path=root_path,
                relative_path=Path(root_path.name),
                label=root_label,
                status=root_status,
                budget=direct_budget,
            )
            byte_limit = min(_MAX_INPUT_SQL_FILE_BYTES, _MAX_INPUT_SQL_BYTES)
            overflow_error = (
                _input_aggregate_limit_error(root_label)
                if _MAX_INPUT_SQL_BYTES < _MAX_INPUT_SQL_FILE_BYTES
                else _input_file_limit_error(root_label)
            )
            descriptor = root_fd
            root_fd = -1
            yield _opened_sql(
                descriptor,
                root_status,
                path=root_path,
                label=root_label,
                read_contents=read_contents,
                byte_limit=byte_limit,
                overflow_error=overflow_error,
            )
            return
        if not stat.S_ISDIR(root_status.st_mode):
            raise TableFactoryError(f"input path does not exist: {root_label}")

        try:
            pending_files = _collect_sql_metadata(
                root_fd,
                root_status,
                root_path=root_path,
                root_label=root_label,
                cwd=cwd,
            )
        except MemoryError:
            raise TableFactoryError(
                f"cannot scan input {root_label}: insufficient memory"
            ) from None
        if not pending_files:
            raise TableFactoryError(f"no SQL files found in input: {root_label}")

        opened_byte_count = 0
        actual_byte_count = 0
        for pending in pending_files:
            file_fd, opened_status = _reopen_scanned_entry(
                root_fd,
                pending.relative_path,
                pending.identity,
                expected="regular",
                label=pending.label,
            )
            try:
                if opened_status.st_size > _MAX_INPUT_SQL_FILE_BYTES:
                    raise _input_file_limit_error(pending.label)
                opened_byte_count += opened_status.st_size
                if opened_byte_count > _MAX_INPUT_SQL_BYTES:
                    raise _input_aggregate_limit_error(root_label)
            except TableFactoryError:
                os.close(file_fd)
                raise

            remaining_bytes = _MAX_INPUT_SQL_BYTES - actual_byte_count
            byte_limit = min(_MAX_INPUT_SQL_FILE_BYTES, remaining_bytes)
            overflow_error = (
                _input_aggregate_limit_error(root_label)
                if remaining_bytes < _MAX_INPUT_SQL_FILE_BYTES
                else _input_file_limit_error(pending.label)
            )
            scanned = _opened_sql(
                file_fd,
                opened_status,
                path=pending.path,
                label=pending.label,
                read_contents=read_contents,
                byte_limit=byte_limit,
                overflow_error=overflow_error,
            )
            if read_contents:
                actual_byte_count += scanned.byte_count
            yield scanned
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def discover_sql_files(input_path: Path, *, cwd: Path) -> tuple[Path, ...]:
    """Find SQL inputs deterministically without depending on the host path."""
    scanned_files = _scan_sql_files(input_path, cwd=cwd, read_contents=False)
    try:
        return tuple(scanned.path for scanned in scanned_files)
    finally:
        scanned_files.close()


def parse_files(input_path: Path, *, cwd: Path) -> tuple[ParsedFile, ...]:
    """Read and parse every input before any output is created."""
    parsed: list[ParsedFile] = []
    scanned_files = _scan_sql_files(input_path, cwd=cwd, read_contents=True)
    try:
        for scanned in scanned_files:
            if scanned.sql is None:
                raise AssertionError("input scanner omitted requested SQL contents")
            try:
                tables = parse_hive_ddl(scanned.sql)
            except DdlParseError as error:
                raise DdlParseError(f"{scanned.label}: {error}") from None
            except MemoryError:
                raise TableFactoryError(
                    f"cannot parse input {scanned.label}: insufficient memory"
                ) from None
            parsed.append(
                ParsedFile(
                    path=scanned.path,
                    label=scanned.label,
                    tables=tables,
                    identity=scanned.identity,
                )
            )
    finally:
        scanned_files.close()
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
    ensure_unique_artifact_names(
        filename for plan in plans for filename in artifact_filenames(plan, config=config)
    )

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
