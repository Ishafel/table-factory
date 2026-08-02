from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

import table_factory.cli as cli_module
import table_factory.workflow as workflow_module
from table_factory.cli import main
from table_factory.config import ARTIFACT_ROLES, FactoryConfig
from table_factory.errors import TableFactoryError
from table_factory.generator import Artifact
from table_factory.models import Table, TablePlan


def _validate(
    input_path: Path,
    config_path: Path,
    *,
    cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    monkeypatch.chdir(cwd)
    exit_code = main(
        [
            "validate",
            "--input",
            str(input_path),
            "--config",
            str(config_path),
        ]
    )
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _assert_controlled_limit_error(exit_code: int, stdout: str, stderr: str) -> None:
    assert exit_code == 2
    assert stdout == ""
    assert stderr.startswith("table-factory: error: ")
    assert "Traceback" not in stderr


def _fail_if_parsed(_sql: str) -> None:
    raise AssertionError("resource limits must be checked before parsing begins")


def test_directory_depth_limit_is_a_controlled_cli_error(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_INPUT_DIRECTORY_DEPTH", 3)
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    nested = input_directory
    for index in range(4):
        nested /= f"level-{index}"
        nested.mkdir()
    (nested / "source.sql").write_text("CREATE TABLE source (id BIGINT);", encoding="utf-8")

    result = _validate(
        input_directory,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    _assert_controlled_limit_error(*result)
    assert "input directory nesting exceeds the supported limit of 3" in result[2]


def test_sql_file_count_limit_is_checked_before_parsing(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_INPUT_SQL_FILES", 2)
    monkeypatch.setattr(workflow_module, "parse_hive_ddl", _fail_if_parsed)
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    for index in range(3):
        (input_directory / f"{index}.sql").write_text("", encoding="utf-8")

    result = _validate(
        input_directory,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    _assert_controlled_limit_error(*result)
    assert "input contains more than 2 SQL files" in result[2]


def test_per_file_byte_limit_is_checked_before_parsing(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_INPUT_SQL_FILE_BYTES", 64)
    monkeypatch.setattr(workflow_module, "parse_hive_ddl", _fail_if_parsed)
    source = tmp_path / "source.sql"
    source.write_bytes(b" " * 65)

    result = _validate(
        source,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    _assert_controlled_limit_error(*result)
    assert "input SQL file exceeds the supported limit of 64 bytes" in result[2]


def test_aggregate_byte_limit_is_checked_before_parsing(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_INPUT_SQL_FILE_BYTES", 80)
    monkeypatch.setattr(workflow_module, "_MAX_INPUT_SQL_BYTES", 100)
    monkeypatch.setattr(workflow_module, "parse_hive_ddl", _fail_if_parsed)
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    for name in ("one.sql", "two.sql"):
        (input_directory / name).write_bytes(b" " * 60)

    result = _validate(
        input_directory,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    _assert_controlled_limit_error(*result)
    assert "input SQL files exceed the supported aggregate limit of 100 bytes" in result[2]


@pytest.mark.parametrize(
    ("file_limit", "aggregate_limit", "error_pattern"),
    [
        (64, 1024, "supported limit of 64 bytes"),
        (128, 64, "supported aggregate limit of 64 bytes"),
    ],
    ids=["per-file", "aggregate"],
)
def test_actual_bytes_read_are_bounded_if_a_file_grows_after_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_limit: int,
    aggregate_limit: int,
    error_pattern: str,
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_INPUT_SQL_FILE_BYTES", file_limit)
    monkeypatch.setattr(workflow_module, "_MAX_INPUT_SQL_BYTES", aggregate_limit)
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    source = input_directory / "source.sql"
    source.write_text("", encoding="utf-8")
    real_open_verified_entry = workflow_module.open_verified_entry
    grown = False

    def grow_after_open(
        directory_fd: int,
        name: str,
        checked_status: os.stat_result,
        *,
        expected: str,
        final_component: bool = False,
    ) -> tuple[int, os.stat_result]:
        nonlocal grown
        descriptor, opened_status = real_open_verified_entry(
            directory_fd,
            name,
            checked_status,
            expected=expected,
            final_component=final_component,
        )
        if expected == "regular" and not grown:
            grown = True
            source.write_bytes(b" " * (min(file_limit, aggregate_limit) + 1))
        return descriptor, opened_status

    monkeypatch.setattr(workflow_module, "open_verified_entry", grow_after_open)

    with pytest.raises(TableFactoryError, match=error_pattern):
        workflow_module.parse_files(input_directory, cwd=tmp_path)


def test_input_contents_are_read_and_parsed_one_file_at_a_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    (input_directory / "a.sql").write_text(
        "CREATE TABLE first_table (id BIGINT);",
        encoding="utf-8",
    )
    (input_directory / "b.sql").write_text(
        "CREATE TABLE second_table (id BIGINT);",
        encoding="utf-8",
    )
    events: list[str] = []
    real_read_utf8 = workflow_module._read_utf8

    def tracked_read_utf8(
        descriptor: int,
        *,
        label: str,
        byte_limit: int,
        overflow_error: TableFactoryError,
    ) -> tuple[str, int]:
        events.append(f"read:{Path(label).name}")
        return real_read_utf8(
            descriptor,
            label=label,
            byte_limit=byte_limit,
            overflow_error=overflow_error,
        )

    def tracked_parse_hive_ddl(
        sql: str,
        **_limits: int | None,
    ) -> tuple[Table, ...]:
        marker = "first" if "first_table" in sql else "second"
        events.append(f"parse:{marker}")
        return ()

    monkeypatch.setattr(workflow_module, "_read_utf8", tracked_read_utf8)
    monkeypatch.setattr(workflow_module, "parse_hive_ddl", tracked_parse_hive_ddl)

    parsed = workflow_module.parse_files(input_directory, cwd=tmp_path)

    assert len(parsed) == 2
    assert events == ["read:a.sql", "parse:first", "read:b.sql", "parse:second"]


def test_parser_memory_error_is_controlled_and_closes_the_scanner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    source = input_directory / "source.sql"
    source.write_text("CREATE TABLE source (id BIGINT);", encoding="utf-8")
    real_scan_sql_files = workflow_module._scan_sql_files
    real_open_input_root = workflow_module._open_input_root
    scanner_closed = False
    root_descriptor: int | None = None

    def tracked_open_input_root(
        input_path: Path,
        *,
        cwd: Path,
    ) -> tuple[int, os.stat_result, Path, str]:
        nonlocal root_descriptor
        result = real_open_input_root(input_path, cwd=cwd)
        root_descriptor = result[0]
        return result

    def tracked_scan_sql_files(
        input_path: Path,
        *,
        cwd: Path,
        read_contents: bool,
    ) -> Generator[workflow_module._ScannedSql, None, None]:
        nonlocal scanner_closed
        scanned_files = real_scan_sql_files(
            input_path,
            cwd=cwd,
            read_contents=read_contents,
        )
        try:
            yield from scanned_files
        finally:
            scanner_closed = True
            scanned_files.close()

    def exhaust_memory(_sql: str, **_limits: int | None) -> None:
        raise MemoryError

    monkeypatch.setattr(workflow_module, "_open_input_root", tracked_open_input_root)
    monkeypatch.setattr(workflow_module, "_scan_sql_files", tracked_scan_sql_files)
    monkeypatch.setattr(workflow_module, "parse_hive_ddl", exhaust_memory)

    with pytest.raises(TableFactoryError, match=r"cannot parse input input/source\.sql"):
        workflow_module.parse_files(input_directory, cwd=tmp_path)

    assert scanner_closed
    assert root_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(root_descriptor)


def test_metadata_collection_memory_error_is_controlled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_directory = tmp_path / "input"
    input_directory.mkdir()

    def exhaust_memory(*_args: object, **_kwargs: object) -> None:
        raise MemoryError

    monkeypatch.setattr(workflow_module, "_collect_sql_metadata", exhaust_memory)

    with pytest.raises(TableFactoryError, match="cannot scan input input: insufficient memory"):
        workflow_module.parse_files(input_directory, cwd=tmp_path)


def test_scanned_entry_limit_bounds_the_iterative_work_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_INPUT_ENTRIES", 2)
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    for index in range(3):
        (input_directory / f"ignored-{index}.txt").write_text("", encoding="utf-8")

    with pytest.raises(TableFactoryError, match="more than 2 filesystem entries"):
        workflow_module.parse_files(input_directory, cwd=tmp_path)


def test_directory_count_limit_bounds_the_iterative_work_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_INPUT_DIRECTORIES", 2)
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    (input_directory / "one").mkdir()
    (input_directory / "two").mkdir()

    with pytest.raises(TableFactoryError, match="more than 2 directories"):
        workflow_module.parse_files(input_directory, cwd=tmp_path)


def test_table_count_limit_is_a_controlled_cli_error(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_TABLES", 1)
    source = tmp_path / "source.sql"
    source.write_text(
        "CREATE TABLE first_table (id BIGINT);\nCREATE TABLE second_table (id BIGINT);\n",
        encoding="utf-8",
    )

    result = _validate(
        source,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    _assert_controlled_limit_error(*result)
    assert "input contains more than 1 tables" in result[2]


def test_column_count_limit_is_a_controlled_cli_error(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_COLUMNS", 2)
    source = tmp_path / "source.sql"
    source.write_text(
        "CREATE TABLE source (first_id BIGINT, second_id BIGINT, third_id BIGINT);",
        encoding="utf-8",
    )

    result = _validate(
        source,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    _assert_controlled_limit_error(*result)
    assert "input contains more than 2 columns" in result[2]


def test_artifact_count_limit_counts_only_enabled_roles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_ARTIFACTS", 1)
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE source (id BIGINT);", encoding="utf-8")
    default_config = FactoryConfig()

    with pytest.raises(TableFactoryError, match="more than 1 artifacts"):
        workflow_module.prepare(source, config=default_config, cwd=tmp_path)

    one_role_config = replace(
        default_config,
        output=replace(
            default_config.output,
            enabled_artifacts=frozenset({ARTIFACT_ROLES[0]}),
        ),
    )
    prepared = workflow_module.prepare(source, config=one_role_config, cwd=tmp_path)

    assert len(prepared.artifacts) == 1


def test_artifact_count_limit_is_a_controlled_cli_error(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_ARTIFACTS", 5)
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE source (id BIGINT);", encoding="utf-8")

    result = _validate(
        source,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    _assert_controlled_limit_error(*result)
    assert "workflow would generate more than 5 artifacts" in result[2]


def test_lightweight_prepare_renders_without_retaining_artifact_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sql"
    source.write_text(
        "CREATE TABLE first_table (id BIGINT);\nCREATE TABLE second_table (id BIGINT);\n",
        encoding="utf-8",
    )
    rendered_tables: list[str] = []
    real_iter_artifacts = workflow_module.iter_artifacts

    def tracked_iter_artifacts(
        table: Table | TablePlan,
        *,
        config: FactoryConfig,
        source_label: str,
    ) -> Iterator[Artifact]:
        source_table = table.source if isinstance(table, TablePlan) else table
        rendered_tables.append(source_table.name)
        yield from real_iter_artifacts(
            table,
            config=config,
            source_label=source_label,
        )

    monkeypatch.setattr(workflow_module, "iter_artifacts", tracked_iter_artifacts)

    prepared = workflow_module.prepare(
        source,
        config=FactoryConfig(),
        cwd=tmp_path,
        retain_artifacts=False,
    )

    assert rendered_tables == ["first_table", "second_table"]
    assert len(prepared.plans) == 2
    assert prepared.artifacts == ()


def test_validate_cli_selects_lightweight_prepare(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE source (id BIGINT);", encoding="utf-8")
    retain_values: list[bool] = []
    real_prepare = cli_module.prepare

    def tracked_prepare(
        input_path: Path,
        *,
        config: FactoryConfig,
        cwd: Path,
        retain_artifacts: bool = True,
    ) -> workflow_module.PreparedWorkflow:
        retain_values.append(retain_artifacts)
        return real_prepare(
            input_path,
            config=config,
            cwd=cwd,
            retain_artifacts=retain_artifacts,
        )

    monkeypatch.setattr(cli_module, "prepare", tracked_prepare)

    result = _validate(
        source,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    assert result[0] == 0
    assert retain_values == [False]


def test_artifact_render_memory_error_is_a_controlled_cli_error(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE source (id BIGINT);", encoding="utf-8")

    def exhaust_memory(*_args: object, **_kwargs: object) -> None:
        raise MemoryError

    monkeypatch.setattr(workflow_module, "iter_artifacts", exhaust_memory)

    result = _validate(
        source,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    _assert_controlled_limit_error(*result)
    assert "cannot prepare workflow: insufficient memory" in result[2]


def test_prepare_memory_error_is_a_controlled_cli_error(
    tmp_path: Path,
    test_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE source (id BIGINT);", encoding="utf-8")

    def exhaust_memory(*_args: object, **_kwargs: object) -> None:
        raise MemoryError

    monkeypatch.setattr(workflow_module, "build_target_names", exhaust_memory)

    result = _validate(
        source,
        test_config_path,
        cwd=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    _assert_controlled_limit_error(*result)
    assert "cannot prepare workflow: insufficient memory" in result[2]


def test_generate_memory_error_after_prepare_is_controlled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE source (id BIGINT);", encoding="utf-8")

    def exhaust_memory(*_args: object, **_kwargs: object) -> None:
        raise MemoryError

    monkeypatch.setattr(workflow_module, "write_artifacts", exhaust_memory)

    with pytest.raises(
        TableFactoryError,
        match="cannot complete workflow: insufficient memory",
    ):
        workflow_module.generate(
            source,
            tmp_path / "output",
            config=FactoryConfig(),
            cwd=tmp_path,
        )
