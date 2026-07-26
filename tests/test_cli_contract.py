from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import invoke_cli, parse_json_output

from table_factory.workflow import display_path


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _generated_sql(output_dir: Path) -> list[Path]:
    return sorted(path for path in output_dir.glob("*.sql") if path.is_file())


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


def test_generate_resolves_relative_unicode_paths_and_writes_five_files(
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
    assert len(generated) == 5
    assert all(path.stat().st_size > 0 for path in generated)
    assert not list(cli_case["output"].rglob("*.tmp"))

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

    assert len(first) == 5
    assert second == first


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
    assert document
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

    invoke_cli(
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

    output_root = cli_case["output"].resolve()
    new_files = {
        path.resolve()
        for path in scan_root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.resolve() not in files_before
    }
    assert all(path.is_relative_to(output_root) for path in new_files)


def test_create_artifact_preserves_table_level_hive_clauses(
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

    create = next(cli_case["output"].glob("*__create.sql")).read_text(encoding="utf-8")
    assert "STORED AS PARQUET" in create


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
