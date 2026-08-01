from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import invoke_cli, parse_json_output

import table_factory.generator as generator_module
import table_factory.path_safety as path_safety_module
import table_factory.workflow as workflow_module
from table_factory.config import ARTIFACT_ROLES
from table_factory.errors import OutputSafetyError, TableFactoryError
from table_factory.generator import Artifact, write_artifacts
from table_factory.path_safety import has_untrusted_symlink_component
from table_factory.workflow import display_path


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _generated_sql(output_dir: Path) -> list[Path]:
    return sorted(path for path in output_dir.glob("*.sql") if path.is_file())


def _configure_artifacts(config_path: Path, enabled: set[str]) -> None:
    contents = config_path.read_text(encoding="utf-8")
    for role in ARTIFACT_ROLES:
        old = f'    "{role}": true'
        assert old in contents
        contents = contents.replace(
            old,
            f'    "{role}": {str(role in enabled).lower()}',
            1,
        )
    config_path.write_text(contents, encoding="utf-8")


def _assert_no_absolute_workspace_path(text: str, root: Path) -> None:
    assert str(root.resolve()) not in text


def test_help_exposes_the_three_documented_commands(tmp_path: Path) -> None:
    result = invoke_cli("--help", cwd=tmp_path)

    assert result.returncode == 0
    for command in ("generate", "validate", "inspect"):
        assert command in result.stdout


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="The platform cannot create symlinks",
)
def test_display_path_handles_a_symlink_loop_without_exposing_the_host(
    tmp_path: Path,
) -> None:
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop)
    except OSError as error:
        pytest.skip(f"Cannot create symlinks in this environment: {error}")

    label = display_path(loop, cwd=tmp_path)

    assert label == "loop"
    _assert_no_absolute_workspace_path(label, tmp_path)


def test_root_owned_system_path_aliases_are_not_treated_as_user_symlinks() -> None:
    aliases = [path for path in (Path("/var"), Path("/tmp")) if path.is_symlink()]
    if aliases:
        assert all(not has_untrusted_symlink_component(path / "folders") for path in aliases)


def test_generate_resolves_relative_unicode_paths_and_writes_six_files(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    result = invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )

    generated = _generated_sql(cli_case["output"])
    assert [path.name for path in generated] == [
        "analytics_customer_orders__01_hive_create_physical.sql",
        "analytics_customer_orders__02_hive_insert.sql",
        "analytics_customer_orders__03_greenplum_create_external.sql",
        "analytics_customer_orders__03_greenplum_create_external_liquibase.sql",
        "analytics_customer_orders__04_greenplum_create_physical.sql",
        "analytics_customer_orders__05_greenplum_insert.sql",
    ]
    assert all(path.stat().st_size > 0 for path in generated)
    assert not list(cli_case["output"].rglob("*.tmp"))
    workflow_sql = "\n".join(
        path.read_text(encoding="utf-8")
        for path in generated
        if "03_greenplum_create_external_liquibase" not in path.name
    ).upper()
    assert "DROP " not in workflow_sql
    assert "DESCRIBE " not in workflow_sql
    assert "SHOW CREATE" not in workflow_sql
    assert "ANALYZE " not in workflow_sql
    assert "SELECT *" not in workflow_sql
    liquibase = generated[3].read_text(encoding="utf-8")
    assert liquibase.startswith("--liquibase formatted sql\n")
    assert liquibase.count("DROP EXTERNAL TABLE IF EXISTS") == 1

    observable_text = result.stdout + result.stderr
    observable_text += "".join(path.read_text(encoding="utf-8") for path in generated)
    _assert_no_absolute_workspace_path(observable_text, root)


def test_generate_is_repeatable_and_does_not_accumulate_outputs(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    arguments = (
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
    )

    invoke_cli(*arguments, cwd=root)
    first = {path.name: path.read_bytes() for path in _generated_sql(cli_case["output"])}
    invoke_cli(*arguments, cwd=root)
    second = {path.name: path.read_bytes() for path in _generated_sql(cli_case["output"])}

    assert len(first) == 6
    assert second == first


def test_disabling_artifacts_does_not_delete_files_from_a_previous_run(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    arguments = (
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
    )
    invoke_cli(*arguments, cwd=root)
    _configure_artifacts(cli_case["config"], {"01_hive_create_physical"})

    result = invoke_cli(*arguments, cwd=root)

    assert "Generated 1 SQL files" in result.stdout
    assert len(_generated_sql(cli_case["output"])) == 6


def test_generate_and_inspect_respect_selective_artifact_config(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    enabled = {
        "03_greenplum_create_external_liquibase",
        "05_greenplum_insert",
    }
    _configure_artifacts(cli_case["config"], enabled)

    generated_result = invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )
    inspected_result = invoke_cli(
        "inspect",
        _relative(cli_case["ddl"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )

    assert "Generated 2 SQL files" in generated_result.stdout
    assert [path.name for path in _generated_sql(cli_case["output"])] == [
        "analytics_customer_orders__03_greenplum_create_external_liquibase.sql",
        "analytics_customer_orders__05_greenplum_insert.sql",
    ]
    effective = parse_json_output(inspected_result)["config"]["output"]["artifacts"]
    assert effective == {role: role in enabled for role in ARTIFACT_ROLES}


def test_generate_allows_every_artifact_to_be_disabled(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    _configure_artifacts(cli_case["config"], set())

    result = invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )

    assert "Generated 0 SQL files" in result.stdout
    assert cli_case["output"].is_dir()
    assert not list(cli_case["output"].iterdir())


def test_generate_and_inspect_support_qualified_name_inside_one_backtick_pair(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    cli_case["ddl"].write_text(
        "CREATE EXTERNAL TABLE `source_db.events` (id BIGINT);\n",
        encoding="utf-8",
    )

    invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )
    inspected = invoke_cli(
        "inspect",
        _relative(cli_case["ddl"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )

    generated = _generated_sql(cli_case["output"])
    assert len(generated) == 6
    assert all(path.name.startswith("source_db_events__") for path in generated)
    hive_insert = next(path for path in generated if "02_hive_insert" in path.name).read_text(
        encoding="utf-8"
    )
    assert "FROM `source_db`.`events`;" in hive_insert
    table = parse_json_output(inspected)["tables"][0]
    assert table["database"] == "source_db"
    assert table["name"] == "events"
    assert table["qualified_name"] == "source_db.events"


def test_generate_does_not_clean_unrelated_existing_outputs(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    cli_case["output"].mkdir()
    unrelated = cli_case["output"] / "keep-this.sql"
    unrelated.write_text("-- user-owned file\n", encoding="utf-8")

    invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )

    assert unrelated.read_text(encoding="utf-8") == "-- user-owned file\n"
    assert len(_generated_sql(cli_case["output"])) == 7


def test_generate_rejects_an_artifact_path_that_is_the_input_ddl(
    tmp_path: Path,
    test_config_path: Path,
) -> None:
    input_path = tmp_path / "db_t__01_hive_create_physical.sql"
    original = b"CREATE TABLE db.t (id BIGINT);\n"
    input_path.write_bytes(original)

    result = invoke_cli(
        "generate",
        "--input",
        input_path,
        "--output",
        tmp_path,
        "--config",
        test_config_path,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 2
    assert "refusing to overwrite an input SQL file" in result.stderr
    assert input_path.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == [input_path.name]
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(
    not hasattr(os, "link"),
    reason="The platform cannot create hard links",
)
def test_generate_rejects_a_destination_that_is_a_hard_link_to_input_ddl(
    tmp_path: Path,
    test_config_path: Path,
) -> None:
    input_directory = tmp_path / "input"
    output_directory = tmp_path / "output"
    input_directory.mkdir()
    output_directory.mkdir()
    input_path = input_directory / "source.sql"
    original = b"CREATE TABLE db.t (id BIGINT);\n"
    input_path.write_bytes(original)

    first_destination = output_directory / "db_t__01_hive_create_physical.sql"
    first_destination.write_bytes(b"existing output must remain unchanged\n")
    hard_link = output_directory / "db_t__05_greenplum_insert.sql"
    try:
        os.link(input_path, hard_link)
    except OSError as error:
        pytest.skip(f"Cannot create hard links in this environment: {error}")
    assert os.path.samefile(input_path, hard_link)

    before = {
        path.name: path.read_bytes()
        for path in sorted(output_directory.iterdir())
        if path.is_file()
    }
    result = invoke_cli(
        "generate",
        "--input",
        input_path,
        "--output",
        output_directory,
        "--config",
        test_config_path,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 2
    assert "refusing to overwrite an input SQL file" in result.stderr
    assert input_path.read_bytes() == original
    assert os.path.samefile(input_path, hard_link)
    assert {
        path.name: path.read_bytes()
        for path in sorted(output_directory.iterdir())
        if path.is_file()
    } == before
    assert not list(output_directory.glob("*.tmp"))
    assert not list(output_directory.glob("*.bak"))
    assert "Traceback" not in result.stderr


def test_validate_accepts_the_example_without_creating_sql(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    result = invoke_cli(
        "validate",
        "--input",
        _relative(cli_case["input"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )

    assert result.returncode == 0
    assert not cli_case["output"].exists()
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


def test_validate_does_not_silently_skip_an_unreadable_input_directory(
    cli_case: dict[str, Path],
) -> None:
    restricted = cli_case["input"] / "недоступный каталог"
    restricted.mkdir()
    (restricted / "скрытая таблица.sql").write_text(
        "CREATE TABLE hidden_table (id BIGINT);\n",
        encoding="utf-8",
    )
    restricted.chmod(0)
    try:
        try:
            next(restricted.iterdir())
        except PermissionError:
            pass
        else:
            pytest.skip("The current user can read directories without permission bits")

        root = cli_case["root"]
        result = invoke_cli(
            "validate",
            "--input",
            _relative(cli_case["input"], root),
            "--config",
            _relative(cli_case["config"], root),
            cwd=root,
            check=False,
        )
    finally:
        restricted.chmod(0o700)

    assert result.returncode != 0
    assert "cannot scan input" in result.stderr
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="The platform cannot create symlinks",
)
def test_validate_does_not_follow_a_sql_symlink_outside_the_input_directory(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    outside = root / "outside.sql"
    outside.write_text("CREATE TABLE outside_table (id BIGINT);\n", encoding="utf-8")
    link = cli_case["input"] / "linked table.sql"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Cannot create symlinks in this environment: {error}")

    result = invoke_cli(
        "validate",
        "--input",
        _relative(cli_case["input"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode != 0
    assert "must not be a symbolic link" in result.stderr
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="The platform cannot create symlinks",
)
def test_validate_rejects_the_final_input_path_when_it_is_a_symlink(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    link = root / "linked input"
    try:
        link.symlink_to(cli_case["input"], target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create directory symlinks in this environment: {error}")

    result = invoke_cli(
        "validate",
        "--input",
        _relative(link, root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode != 0
    assert "input path must not be a symbolic link" in result.stderr
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="The platform cannot create symlinks",
)
def test_validate_rejects_a_symlink_parent_component_of_the_input(
    cli_case: dict[str, Path],
    tmp_path: Path,
) -> None:
    root = cli_case["root"]
    outside_parent = tmp_path / "outside parent"
    outside_input = outside_parent / "real input"
    outside_input.mkdir(parents=True)
    (outside_input / "outside.sql").write_text(
        "CREATE TABLE outside_table (id BIGINT);\n",
        encoding="utf-8",
    )
    link = root / "linked parent"
    try:
        link.symlink_to(outside_parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create directory symlinks in this environment: {error}")

    result = invoke_cli(
        "validate",
        "--input",
        str(Path(link.name) / outside_input.name),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode != 0
    assert "input path must not contain a symbolic-link component" in result.stderr
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="The platform cannot create symlinks",
)
def test_validate_rejects_a_nested_input_directory_symlink(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    outside = root / "outside input"
    outside.mkdir()
    (outside / "outside.sql").write_text(
        "CREATE TABLE outside_table (id BIGINT);\n",
        encoding="utf-8",
    )
    link = cli_case["input"] / "linked directory"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create directory symlinks in this environment: {error}")

    result = invoke_cli(
        "validate",
        "--input",
        _relative(cli_case["input"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode != 0
    assert "input directory must not contain a symbolic link" in result.stderr
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


def test_cli_treats_a_tilde_path_as_relative_to_the_current_directory(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    result = invoke_cli(
        "validate",
        "--input",
        "~unknown-table-factory-user/input",
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode == 2
    assert "input path does not exist: ~unknown-table-factory-user/input" in result.stderr
    assert "Traceback" not in result.stderr
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


def test_inspect_returns_json_and_keeps_host_paths_private(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    result = invoke_cli(
        "inspect",
        _relative(cli_case["ddl"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )

    document = parse_json_output(result)
    assert isinstance(document, dict)
    assert document["config"]["version"] == 3
    assert document["config"]["output"]["artifacts"] == {role: True for role in ARTIFACT_ROLES}
    assert document["config"]["hive"]["replica"] == "replica"
    assert document["config"]["greenplum"]["replica"] == "replica"
    assert document["config"]["greenplum"]["subscription"] == "subscription"
    assert document["config"]["greenplum"]["original_hive_database"] == "original_hive_database"
    table = document["tables"][0]
    assert table["source_path"] == "входные DDL/пример таблицы.sql"
    assert table["qualified_name"] == "analytics.customer_orders"
    assert table["external"] is True
    assert [column["name"] for column in table["partition_columns"]] == ["business_date"]
    assert table["targets"]["hive"]["qualified_name"] == (
        "target_hive_db.replica_customer_orders_physical"
    )
    assert table["targets"]["greenplum"]["external"]["qualified_name"] == (
        "ext.replica_customer_orders_ext"
    )
    assert table["targets"]["greenplum"]["physical"]["qualified_name"] == (
        "dwh.replica_customer_orders"
    )
    assert [column["greenplum_type"] for column in table["greenplum_columns"]] == [
        "BIGINT",
        "TEXT",
        "TIMESTAMP",
        "NUMERIC(18,2)",
        "DATE",
    ]
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


def test_inspect_rejects_a_directory_instead_of_dropping_documents(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    result = invoke_cli(
        "inspect",
        _relative(cli_case["input"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode != 0
    assert "one SQL file" in result.stderr


def test_invalid_ddl_fails_without_partial_outputs(
    cli_case: dict[str, Path],
) -> None:
    cli_case["ddl"].write_text(
        "THIS IS NOT A CREATE TABLE STATEMENT;\n",
        encoding="utf-8",
    )
    root = cli_case["root"]

    result = invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode != 0
    assert not cli_case["output"].exists() or not list(cli_case["output"].rglob("*.sql"))
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


def test_table_name_cannot_escape_the_output_directory(
    cli_case: dict[str, Path],
) -> None:
    cli_case["ddl"].write_text(
        "CREATE TABLE `../../outside` (`id` BIGINT) STORED AS PARQUET;\n",
        encoding="utf-8",
    )
    root = cli_case["root"]
    scan_root = root.parent
    files_before = {
        path.resolve() for path in scan_root.rglob("*") if path.is_file() and not path.is_symlink()
    }

    result = invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode == 2
    assert "table names may contain at most one database qualifier" in result.stderr
    assert not cli_case["output"].exists()
    output_root = cli_case["output"].resolve()
    new_files = {
        path.resolve()
        for path in scan_root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.resolve() not in files_before
    }
    assert all(path.is_relative_to(output_root) for path in new_files)


def test_hive_physical_artifact_replaces_source_storage_and_partitioning(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
    )

    create = next(cli_case["output"].glob("*__01_hive_create_physical.sql")).read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE `target_hive_db`.`replica_customer_orders_physical`" in create
    assert "`business_date` DATE COMMENT 'Source partition date'" in create
    assert "STORED AS TEXTFILE" in create
    assert "FIELDS TERMINATED BY ','" in create
    assert "ESCAPED BY '\\\\'" in create
    assert "NULL DEFINED AS '\\\\N'" in create
    assert "EXTERNAL" not in create
    assert "PARTITIONED BY" not in create
    assert "STORED AS PARQUET" not in create
    assert "LOCATION" not in create
    assert "TBLPROPERTIES" not in create


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="The platform cannot create symlinks",
)
def test_generate_never_follows_an_output_symlink(
    cli_case: dict[str, Path],
) -> None:
    root = cli_case["root"]
    arguments = (
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        _relative(cli_case["output"], root),
        "--config",
        _relative(cli_case["config"], root),
    )
    invoke_cli(*arguments, cwd=root)
    target = _generated_sql(cli_case["output"])[0]
    outside = root / "outside-sentinel.sql"
    sentinel = b"must not be overwritten\n"
    outside.write_bytes(sentinel)
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Cannot create symlinks in this environment: {error}")

    result = invoke_cli(*arguments, cwd=root, check=False)

    assert outside.read_bytes() == sentinel
    if result.returncode == 0:
        assert not target.is_symlink()


def test_output_directory_symlink_is_rejected(
    cli_case: dict[str, Path],
    tmp_path: Path,
) -> None:
    root = cli_case["root"]
    outside = tmp_path / "outside output"
    outside.mkdir()
    link = root / "linked output"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create directory symlinks in this environment: {error}")

    result = invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        link.name,
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode != 0
    assert not list(outside.iterdir())


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="The platform cannot create symlinks",
)
def test_output_path_with_a_symlink_parent_component_is_rejected(
    cli_case: dict[str, Path],
    tmp_path: Path,
) -> None:
    root = cli_case["root"]
    outside = tmp_path / "outside parent"
    outside.mkdir()
    link = root / "linked parent"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create directory symlinks in this environment: {error}")

    result = invoke_cli(
        "generate",
        "--input",
        _relative(cli_case["input"], root),
        "--output",
        str(Path(link.name) / "nested output"),
        "--config",
        _relative(cli_case["config"], root),
        cwd=root,
        check=False,
    )

    assert result.returncode != 0
    assert "output path must not contain a symbolic-link component" in result.stderr
    assert not list(outside.iterdir())
    _assert_no_absolute_workspace_path(result.stdout + result.stderr, root)


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="The platform cannot create symlinks",
)
def test_write_artifacts_pins_the_accepted_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    accepted_output = tmp_path / "accepted-output"
    outside = tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    outside_destination = outside / "escaped.sql"
    outside_destination.write_text("outside sentinel\n", encoding="utf-8")
    artifact = Artifact(filename="escaped.sql", content="generated\n")

    real_write_staged_file = generator_module._write_staged_file
    swapped = False

    def swap_output_after_it_is_pinned(
        output_directory_fd: int,
        pending_artifact: Artifact,
    ) -> str:
        nonlocal swapped
        if not swapped:
            swapped = True
            output.rename(accepted_output)
            output.symlink_to(outside, target_is_directory=True)
        return real_write_staged_file(output_directory_fd, pending_artifact)

    monkeypatch.setattr(generator_module, "_write_staged_file", swap_output_after_it_is_pinned)

    write_artifacts(output, [artifact])

    assert (accepted_output / artifact.filename).read_text(encoding="utf-8") == "generated\n"
    assert outside_destination.read_text(encoding="utf-8") == "outside sentinel\n"
    assert not list(outside.glob("*.tmp"))
    assert not list(outside.glob("*.bak"))


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="The platform cannot create symlinks",
)
def test_parse_files_reads_from_the_pinned_input_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_directory = tmp_path / "input"
    accepted_input = tmp_path / "accepted-input"
    outside = tmp_path / "outside"
    input_directory.mkdir()
    outside.mkdir()
    source = input_directory / "source.sql"
    source.write_text("CREATE TABLE original_table (id BIGINT);\n", encoding="utf-8")
    (outside / source.name).write_text(
        "CREATE TABLE outside_table (id BIGINT);\n",
        encoding="utf-8",
    )

    real_open_verified_entry = workflow_module.open_verified_entry
    swapped = False

    def swap_input_after_it_is_pinned(
        directory_fd: int,
        name: str,
        checked_status: os.stat_result,
        *,
        expected: str,
        final_component: bool = False,
    ) -> tuple[int, os.stat_result]:
        nonlocal swapped
        if name == source.name and expected == "regular" and not swapped:
            swapped = True
            input_directory.rename(accepted_input)
            input_directory.symlink_to(outside, target_is_directory=True)
        return real_open_verified_entry(
            directory_fd,
            name,
            checked_status,
            expected=expected,
            final_component=final_component,
        )

    monkeypatch.setattr(workflow_module, "open_verified_entry", swap_input_after_it_is_pinned)

    parsed = workflow_module.parse_files(input_directory, cwd=tmp_path)

    assert parsed[0].tables[0].name == "original_table"
    accepted_status = (accepted_input / source.name).stat()
    assert parsed[0].identity.device == accepted_status.st_dev
    assert parsed[0].identity.inode == accepted_status.st_ino


def test_parse_files_rejects_an_input_file_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    source = input_directory / "source.sql"
    accepted_source = input_directory / "accepted-source.sql"
    replacement = tmp_path / "replacement.sql"
    source.write_text("CREATE TABLE original_table (id BIGINT);\n", encoding="utf-8")
    replacement.write_text("CREATE TABLE replacement_table (id BIGINT);\n", encoding="utf-8")

    real_open_verified_entry = workflow_module.open_verified_entry
    swapped = False

    def swap_file_between_stat_and_open(
        directory_fd: int,
        name: str,
        checked_status: os.stat_result,
        *,
        expected: str,
        final_component: bool = False,
    ) -> tuple[int, os.stat_result]:
        nonlocal swapped
        if name == source.name and expected == "regular" and not swapped:
            swapped = True
            source.rename(accepted_source)
            os.replace(replacement, source)
        return real_open_verified_entry(
            directory_fd,
            name,
            checked_status,
            expected=expected,
            final_component=final_component,
        )

    monkeypatch.setattr(workflow_module, "open_verified_entry", swap_file_between_stat_and_open)

    with pytest.raises(TableFactoryError, match="cannot read input"):
        workflow_module.parse_files(input_directory, cwd=tmp_path)

    assert accepted_source.read_text(encoding="utf-8").startswith("CREATE TABLE original_table")


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="The platform cannot create FIFOs",
)
def test_parse_files_cannot_block_on_a_file_replaced_by_a_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_directory = tmp_path / "input"
    input_directory.mkdir()
    source = input_directory / "source.sql"
    accepted_source = input_directory / "accepted-source.sql"
    source.write_text("CREATE TABLE original_table (id BIGINT);\n", encoding="utf-8")

    real_open_flags = path_safety_module._open_flags
    swapped = False

    def replace_file_with_fifo_before_open(
        *,
        expected: str,
        nofollow: bool,
    ) -> int:
        nonlocal swapped
        flags = real_open_flags(expected=expected, nofollow=nofollow)
        if expected == "regular" and not swapped:
            swapped = True
            source.rename(accepted_source)
            os.mkfifo(source)
            assert flags & os.O_NONBLOCK
        return flags

    monkeypatch.setattr(path_safety_module, "_open_flags", replace_file_with_fifo_before_open)

    with pytest.raises(TableFactoryError, match="cannot read input"):
        workflow_module.parse_files(input_directory, cwd=tmp_path)


@pytest.mark.skipif(
    not hasattr(os, "symlink") or not hasattr(os, "link"),
    reason="The platform cannot create symlinks and hard links",
)
def test_write_artifacts_rejects_a_legacy_symlink_input_alias(
    tmp_path: Path,
) -> None:
    input_directory = tmp_path / "input"
    output_directory = tmp_path / "output"
    input_directory.mkdir()
    output_directory.mkdir()
    source = input_directory / "source.sql"
    source.write_text("CREATE TABLE original_table (id BIGINT);\n", encoding="utf-8")
    input_link = input_directory / "source-link.sql"
    input_link.symlink_to(source)
    destination = output_directory / "artifact.sql"
    os.link(source, destination)

    with pytest.raises(OutputSafetyError, match="cannot verify an input SQL file"):
        write_artifacts(
            output_directory,
            [Artifact(filename=destination.name, content="generated\n")],
            input_paths=(input_link,),
        )

    assert os.path.samefile(source, destination)
    assert source.read_text(encoding="utf-8").startswith("CREATE TABLE original_table")


def test_write_artifacts_restores_the_whole_set_when_second_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    first = output / "first.sql"
    second = output / "second.sql"
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    artifacts = [
        Artifact(filename=first.name, content="new first\n"),
        Artifact(filename=second.name, content="new second\n"),
    ]

    real_replace = os.replace
    staged_commits = 0

    def fail_second_staged_commit(
        source: str | Path,
        destination: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal staged_commits
        if str(source).endswith(".tmp"):
            staged_commits += 1
            if staged_commits == 2:
                raise OSError("simulated second commit failure")
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", fail_second_staged_commit)

    with pytest.raises(OutputSafetyError, match=r"second\.sql"):
        write_artifacts(output, artifacts)

    assert first.read_text(encoding="utf-8") == "old first\n"
    assert second.read_text(encoding="utf-8") == "old second\n"
    assert sorted(path.name for path in output.iterdir()) == [
        "first.sql",
        "second.sql",
    ]


def test_write_artifacts_reports_backup_cleanup_failure_without_raw_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    destination = output / "artifact.sql"
    destination.write_text("old\n", encoding="utf-8")
    artifact = Artifact(filename=destination.name, content="new\n")

    real_unlink = os.unlink
    backup_unlinks = 0

    def fail_committed_backup_cleanup(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal backup_unlinks
        if str(path).endswith(".bak"):
            backup_unlinks += 1
            if backup_unlinks == 1:
                raise PermissionError(13, "Permission denied", str(path))
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", fail_committed_backup_cleanup)

    with pytest.raises(
        OutputSafetyError,
        match=r"files were replaced.*transaction cleanup failed.*artifact\.sql",
    ):
        write_artifacts(output, [artifact])

    assert destination.read_text(encoding="utf-8") == "new\n"
    backups = list(output.glob("*.bak"))
    assert len(backups) == 1
    backups[0].unlink()
