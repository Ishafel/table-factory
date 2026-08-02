from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT, TEST_CONFIG_PATH

import table_factory.generator as generator_module
from table_factory.config import load_config
from table_factory.errors import ConfigurationError, OutputSafetyError
from table_factory.generator import Artifact, write_artifacts


def _write_config_with_changeset_template(tmp_path: Path, template: str) -> Path:
    contents = TEST_CONFIG_PATH.read_text(encoding="utf-8")
    original = '      changeset_id_template: "stg-{source_table}_ext"'
    assert original in contents
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        contents.replace(
            original,
            f'      changeset_id_template: "{template}"',
            1,
        ),
        encoding="utf-8",
    )
    return config_path


def _invoke_in_ascii_locale(
    *arguments: str | Path,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONIOENCODING", None)
    environment.update(
        {
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONUTF8": "0",
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "table_factory.cli",
            *(str(argument) for argument in arguments),
        ],
        cwd=cwd,
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("command", ("generate", "inspect"))
def test_cli_fails_fast_when_the_process_streams_are_not_utf8(
    tmp_path: Path,
    command: str,
) -> None:
    ddl_path = tmp_path / "input.sql"
    ddl_path.write_text("CREATE TABLE таблица (id BIGINT);\n", encoding="utf-8")
    output_path = tmp_path / "output"

    if command == "generate":
        arguments = (
            "generate",
            "--input",
            ddl_path,
            "--output",
            output_path,
            "--config",
            TEST_CONFIG_PATH,
        )
    else:
        arguments = (
            "inspect",
            ddl_path,
            "--config",
            TEST_CONFIG_PATH,
        )

    result = _invoke_in_ascii_locale(*arguments, cwd=tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "UTF-8 runtime is required" in result.stderr
    assert "set PYTHONUTF8=1" in result.stderr
    assert "UnicodeEncodeError" not in result.stderr
    assert "Traceback" not in result.stderr
    assert not output_path.exists()


def test_unencodable_artifact_filename_is_a_controlled_output_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = "таблица.sql"
    encoding_error = UnicodeEncodeError(
        "ascii",
        filename,
        0,
        len(filename),
        "ordinal not in range",
    )

    def reject_filename(_value: str) -> bytes:
        raise encoding_error

    with monkeypatch.context() as patch:
        patch.setattr(generator_module.os, "fsencode", reject_filename)
        with pytest.raises(OutputSafetyError, match="UTF-8 is required"):
            write_artifacts(
                tmp_path,
                [Artifact(filename=filename, content="SELECT 1;\n")],
            )

    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize(
    ("yaml_escape", "unsafe_character"),
    (
        (r"\u009b", "\u009b"),
        (r"\u200b", "\u200b"),
        (r"\u2028", "\u2028"),
        (r"\u2029", "\u2029"),
    ),
)
def test_configuration_rejects_unsafe_unicode_text_categories(
    tmp_path: Path,
    yaml_escape: str,
    unsafe_character: str,
) -> None:
    config_path = _write_config_with_changeset_template(
        tmp_path,
        f"ok{{evil{yaml_escape}31m}}",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_config(config_path, display_name=config_path.name)

    message = str(captured.value)
    assert "must not contain control characters" in message
    assert unsafe_character not in message


def test_unknown_placeholder_name_is_sanitized_before_diagnostic_output(
    tmp_path: Path,
) -> None:
    config_path = _write_config_with_changeset_template(
        tmp_path,
        r"ok{evil\u00a031m}",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_config(config_path, display_name=config_path.name)

    message = str(captured.value)
    assert message.endswith(r"contains unknown placeholder: evil\u00a031m")
    assert "\u00a0" not in message


def test_docker_runtime_enables_python_utf8_mode() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "PYTHONUTF8=1" in dockerfile
