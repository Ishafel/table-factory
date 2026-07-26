from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "table-factory.yaml"
EXAMPLES_DIR = PROJECT_ROOT / "examples"


class CliResult(subprocess.CompletedProcess[str]):
    """Typed alias for a completed CLI subprocess."""


def invoke_cli(
    *arguments: str | Path,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real module entry point from an arbitrary working directory."""
    command = [
        sys.executable,
        "-m",
        "table_factory.cli",
        *(str(argument) for argument in arguments),
    ]
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        pytest.fail(
            "CLI command failed.\n"
            f"command: {command!r}\n"
            f"cwd: {cwd}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def parse_json_output(result: subprocess.CompletedProcess[str]) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            "inspect must write one valid JSON document to stdout.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        ) from error


@pytest.fixture(scope="session")
def repository_config() -> Path:
    assert CONFIG_PATH.is_file(), f"Missing repository config: {CONFIG_PATH}"
    return CONFIG_PATH


@pytest.fixture(scope="session")
def example_ddl() -> Path:
    examples = sorted(EXAMPLES_DIR.glob("*.sql"))
    assert examples, f"At least one safe Hive DDL example is required in {EXAMPLES_DIR}"
    return examples[0]


@pytest.fixture
def cli_case(
    tmp_path: Path,
    repository_config: Path,
    example_ddl: Path,
) -> dict[str, Path]:
    """A case whose every CLI path contains both spaces and non-ASCII characters."""
    root = tmp_path / "проект с пробелами"
    input_dir = root / "входные DDL"
    output_dir = root / "готовые SQL"
    config_dir = root / "настройки проекта"
    input_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)

    ddl_path = input_dir / "пример таблицы.sql"
    config_path = config_dir / "табличная фабрика.yaml"
    shutil.copyfile(example_ddl, ddl_path)
    shutil.copyfile(repository_config, config_path)

    return {
        "root": root,
        "input": input_dir,
        "output": output_dir,
        "config": config_path,
        "ddl": ddl_path,
    }
