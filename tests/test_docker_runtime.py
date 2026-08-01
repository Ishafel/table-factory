from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT, TEST_CONFIG_PATH

RUN_DOCKER_TESTS = os.environ.get("TABLE_FACTORY_RUN_DOCKER_TESTS") == "1"
DOCKER_SKIP_REASON = (
    "Set TABLE_FACTORY_RUN_DOCKER_TESTS=1 to run the opt-in Docker acceptance suite"
)

pytestmark = pytest.mark.skipif(not RUN_DOCKER_TESTS, reason=DOCKER_SKIP_REASON)


def _docker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    uid = str(os.getuid()) if hasattr(os, "getuid") else "1000"
    gid = str(os.getgid()) if hasattr(os, "getgid") else "1000"
    for variable in ("TABLE_FACTORY_UID", "LOCAL_UID", "HOST_UID", "UID"):
        environment.setdefault(variable, uid)
    for variable in ("TABLE_FACTORY_GID", "LOCAL_GID", "HOST_GID", "GID"):
        environment.setdefault(variable, gid)
    return environment


def _compose(
    *arguments: str,
    check: bool = True,
    timeout: int = 600,
    environment_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", *arguments]
    environment = _docker_environment()
    if environment_overrides is not None:
        environment.update(environment_overrides)
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        pytest.fail(
            "Docker Compose command failed.\n"
            f"command: {command!r}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


@pytest.fixture(scope="session", autouse=True)
def built_development_image() -> None:
    if not RUN_DOCKER_TESTS:
        return
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when TABLE_FACTORY_RUN_DOCKER_TESTS=1")
    _compose("config", "--quiet")
    _compose("build", "table-factory", timeout=1_200)


@pytest.fixture
def mounted_case(example_ddl: Path) -> Iterator[dict[str, Path]]:
    parent = PROJECT_ROOT / "work" / "output"
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / f".acceptance-{uuid.uuid4().hex}"
    input_dir = root / "input with spaces"
    editable_output = root / "docker output"
    wheel_output = root / "wheel output"
    input_dir.mkdir(parents=True)
    ddl = input_dir / "таблица пример.sql"
    config = root / "table-factory.yaml"
    shutil.copyfile(example_ddl, ddl)
    shutil.copyfile(TEST_CONFIG_PATH, config)
    try:
        yield {
            "root": root,
            "input": input_dir,
            "editable_output": editable_output,
            "wheel_output": wheel_output,
            "ddl": ddl,
            "config": config,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _container_relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


def _output_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes() for path in sorted(directory.glob("*.sql")) if path.is_file()
    }


def test_development_image_has_python_312_and_required_tools() -> None:
    probes = (
        ("python", "--version"),
        ("pytest", "--version"),
        ("ruff", "--version"),
        ("mypy", "--version"),
        ("python", "-m", "build", "--version"),
    )
    results: dict[tuple[str, ...], str] = {}

    for probe in probes:
        entrypoint, *arguments = probe
        result = _compose(
            "run",
            "--rm",
            "--entrypoint",
            entrypoint,
            "table-factory",
            *arguments,
        )
        results[probe] = result.stdout + result.stderr

    assert "Python 3.12" in results[("python", "--version")]


def test_development_package_is_editable_and_container_is_non_root() -> None:
    location = _compose(
        "run",
        "--rm",
        "--entrypoint",
        "python",
        "table-factory",
        "-c",
        "import pathlib, table_factory; print(pathlib.Path(table_factory.__file__).resolve())",
    )
    assert "/workspace/" in location.stdout
    assert "site-packages/table_factory" not in location.stdout

    uid = _compose(
        "run",
        "--rm",
        "--entrypoint",
        "id",
        "table-factory",
        "-u",
    )
    assert int(uid.stdout.strip()) != 0


def test_root_host_ids_cannot_override_the_image_user_or_build_a_root_image() -> None:
    root_environment = {"LOCAL_UID": "0", "LOCAL_GID": "0"}
    uid = _compose(
        "run",
        "--rm",
        "--entrypoint",
        "id",
        "table-factory",
        "-u",
        environment_overrides=root_environment,
    )
    assert int(uid.stdout.strip()) != 0

    rejected_build = _compose(
        "build",
        "table-factory",
        check=False,
        timeout=1_200,
        environment_overrides=root_environment,
    )
    assert rejected_build.returncode != 0
    assert "APP_UID and APP_GID must be non-zero" in (rejected_build.stdout + rejected_build.stderr)


def test_documented_quality_commands_run_inside_the_container() -> None:
    commands = (
        ("pytest", "-q"),
        ("ruff", "check", "."),
        ("ruff", "format", "--check", "."),
        ("mypy", "src"),
    )
    for entrypoint, *arguments in commands:
        _compose(
            "run",
            "--rm",
            "--entrypoint",
            entrypoint,
            "table-factory",
            *arguments,
        )


def test_compose_cli_generates_six_persistent_host_owned_files(
    mounted_case: dict[str, Path],
) -> None:
    result = _compose(
        "run",
        "--rm",
        "table-factory",
        "generate",
        "--input",
        _container_relative(mounted_case["input"]),
        "--output",
        _container_relative(mounted_case["editable_output"]),
        "--config",
        _container_relative(mounted_case["config"]),
    )

    generated = _output_bytes(mounted_case["editable_output"])
    assert result.returncode == 0
    assert list(generated) == [
        "analytics_customer_orders__01_hive_create_physical.sql",
        "analytics_customer_orders__02_hive_insert.sql",
        "analytics_customer_orders__03_greenplum_create_external.sql",
        "analytics_customer_orders__03_greenplum_create_external_liquibase.sql",
        "analytics_customer_orders__04_greenplum_create_physical.sql",
        "analytics_customer_orders__05_greenplum_insert.sql",
    ]
    assert all(generated.values())
    if sys.platform.startswith("linux") and hasattr(os, "getuid"):
        assert {path.stat().st_uid for path in mounted_case["editable_output"].glob("*.sql")} == {
            os.getuid()
        }


def test_compose_cli_validates_and_inspects_mounted_unicode_input(
    mounted_case: dict[str, Path],
) -> None:
    validated = _compose(
        "run",
        "--rm",
        "table-factory",
        "validate",
        "--input",
        _container_relative(mounted_case["input"]),
        "--config",
        _container_relative(mounted_case["config"]),
    )
    inspected = _compose(
        "run",
        "--rm",
        "table-factory",
        "inspect",
        _container_relative(mounted_case["ddl"]),
        "--config",
        _container_relative(mounted_case["config"]),
    )

    assert "Validated 1 table(s) in 1 file(s)." in validated.stdout
    document = json.loads(inspected.stdout)
    assert document["tables"][0]["qualified_name"] == "analytics.customer_orders"
    assert str(PROJECT_ROOT.resolve()) not in inspected.stdout + inspected.stderr


def test_wheel_installs_cleanly_and_matches_the_editable_cli(
    mounted_case: dict[str, Path],
) -> None:
    dist = mounted_case["root"] / "dist"
    _compose(
        "run",
        "--rm",
        "--entrypoint",
        "python",
        "table-factory",
        "-m",
        "build",
        "--outdir",
        _container_relative(dist),
        ".",
    )

    wheels = sorted(dist.glob("table_factory-*-py3-none-any.whl"))
    sdists = sorted(dist.glob("table_factory-*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1

    _compose(
        "run",
        "--rm",
        "table-factory",
        "generate",
        "--input",
        _container_relative(mounted_case["input"]),
        "--output",
        _container_relative(mounted_case["editable_output"]),
        "--config",
        _container_relative(mounted_case["config"]),
    )

    wheel_in_container = Path("/workspace") / wheels[0].relative_to(PROJECT_ROOT)
    case_in_container = Path("/workspace") / mounted_case["root"].relative_to(PROJECT_ROOT)
    venv = f"/tmp/table-factory-wheel-{uuid.uuid4().hex}"
    script = " && ".join(
        (
            f"python -m venv {shlex.quote(venv)}",
            f"{shlex.quote(venv)}/bin/pip install --force-reinstall "
            f"{shlex.quote(str(wheel_in_container))}",
            f"cd {shlex.quote(str(case_in_container))}",
            f"{shlex.quote(venv)}/bin/table-factory generate "
            f"--input {shlex.quote(mounted_case['input'].name)} "
            f"--output {shlex.quote(mounted_case['wheel_output'].name)} "
            f"--config {shlex.quote(mounted_case['config'].name)}",
            f"{shlex.quote(venv)}/bin/python -c "
            '"import table_factory; print(table_factory.__file__)"',
        )
    )
    installed = _compose(
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        "table-factory",
        "-c",
        script,
    )

    assert "/workspace/src/table_factory" not in installed.stdout
    assert _output_bytes(mounted_case["wheel_output"]) == _output_bytes(
        mounted_case["editable_output"]
    )
