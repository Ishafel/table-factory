from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml
from conftest import PROJECT_ROOT

from table_factory.config import load_config

RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "table-factory.yaml"

REQUIRED_FILES = (
    "Dockerfile",
    "compose.yaml",
    "config/table-factory.yaml",
    "tests/fixtures/table-factory.yaml",
    ".dockerignore",
    ".gitignore",
    "pyproject.toml",
)


def _compose() -> dict[str, Any]:
    document = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _volume_source_and_target(volume: Any) -> tuple[str, str]:
    if isinstance(volume, str):
        fields = volume.split(":")
        assert len(fields) >= 2, f"Invalid short volume syntax: {volume!r}"
        return fields[0], fields[1]
    assert isinstance(volume, dict), f"Invalid volume definition: {volume!r}"
    assert volume.get("type") == "bind"
    return str(volume.get("source", "")), str(volume.get("target", ""))


def _entrypoint_tokens(entrypoint: Any) -> list[str]:
    if isinstance(entrypoint, str):
        return shlex.split(entrypoint)
    assert isinstance(entrypoint, list)
    return [str(token) for token in entrypoint]


def test_required_project_files_exist() -> None:
    missing = [name for name in REQUIRED_FILES if not (PROJECT_ROOT / name).is_file()]
    assert not missing, f"Missing required project files: {missing}"


def test_runtime_config_is_valid_and_uses_current_version() -> None:
    runtime_config = load_config(
        RUNTIME_CONFIG_PATH,
        display_name="config/table-factory.yaml",
    )

    assert runtime_config.version == 3


def test_compose_defines_one_transient_bind_mounted_service() -> None:
    document = _compose()
    services = document.get("services")
    assert isinstance(services, dict)
    assert set(services) == {"table-factory"}
    service = services["table-factory"]

    build = service.get("build")
    assert isinstance(build, dict)
    assert build.get("context") in (".", "./")
    assert build.get("target") == "development"
    assert service.get("working_dir") == "/workspace"
    assert _entrypoint_tokens(service.get("entrypoint")) == ["table-factory"]
    assert "user" not in service, "Compose must not override the image's non-root USER"
    assert build.get("args") == {
        "APP_UID": "${LOCAL_UID:-1000}",
        "APP_GID": "${LOCAL_GID:-1000}",
    }

    volumes = service.get("volumes")
    assert isinstance(volumes, list)
    mounts = [_volume_source_and_target(volume) for volume in volumes]
    assert any(source in (".", "./") and target == "/workspace" for source, target in mounts)
    assert all(not Path(source).is_absolute() for source, _ in mounts if source)

    assert not document.get("volumes"), "Named Docker volumes are not part of this workflow"
    assert "depends_on" not in service
    assert "restart" not in service


def test_compose_contains_no_database_or_data_platform_services() -> None:
    text = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8").lower()
    forbidden = (
        "hive",
        "hadoop",
        "greenplum",
        "postgres",
        "pxf",
        "mysql",
        "mariadb",
        "mongodb",
    )
    assert not [word for word in forbidden if word in text]


def test_dockerfile_is_python_312_multistage_with_editable_dev_install() -> None:
    text = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    logical_lines = re.sub(r"\\\s*\n", " ", text)
    stages = re.findall(
        r"^\s*FROM\s+(\S+)(?:\s+AS\s+([A-Za-z0-9_.-]+))?",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    assert len(stages) >= 2
    assert any("python:3.12" in image.lower() for image, _ in stages)
    assert any(name.lower() == "development" for _, name in stages)
    assert re.search(
        r"\bWORKDIR\s+/workspace\b",
        text,
        flags=re.IGNORECASE,
    )
    assert re.search(
        r"(?:python\s+-m\s+)?pip\s+install\b[^;\n]*"
        r"-e\s+[\"']?\.\[dev\][\"']?",
        logical_lines,
        flags=re.IGNORECASE,
    )
    assert "APP_UID and APP_GID must be non-zero" in text
    assert re.search(
        r"^\s*USER\s+\$\{APP_UID\}:\$\{APP_GID\}\s*$",
        text,
        flags=re.MULTILINE,
    )


def test_docker_context_excludes_local_and_generated_state() -> None:
    lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    def excludes(path: str) -> bool:
        directory_forms = {path, f"{path}/", f"{path}/*", f"{path}/**"}
        return bool(lines & directory_forms)

    required = (
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "dist",
        "work/input",
        "work/output",
    )
    assert not [path for path in required if not excludes(path)]


def test_gitignore_preserves_placeholders_but_ignores_work_products() -> None:
    required_lines = {
        "work/input/*",
        "work/output/*",
        "dist/",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        "__pycache__/",
        "*.egg-info/",
    }
    lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert required_lines <= lines

    for relative in ("work/input/.gitkeep", "work/output/.gitkeep"):
        placeholder = PROJECT_ROOT / relative
        assert placeholder.is_file()
        assert f"!{relative}" in lines

    # A slim development image is not required to carry Git. Where Git is
    # available, also verify its real ignore matcher rather than only the rules.
    if shutil.which("git"):
        for relative in ("work/input/example.sql", "work/output/generated.sql", "dist/pkg.whl"):
            ignored = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", relative],
                cwd=PROJECT_ROOT,
                check=False,
            )
            assert ignored.returncode == 0, f"{relative} should be ignored"


def test_repository_contains_a_safe_hive_ddl_example() -> None:
    examples = sorted((PROJECT_ROOT / "examples").glob("*.sql"))
    assert examples
    for example in examples:
        sql = example.read_text(encoding="utf-8")
        normalized = re.sub(r"--.*?$|/\*.*?\*/", "", sql, flags=re.MULTILINE | re.DOTALL)
        assert re.search(r"\bCREATE\s+(?:EXTERNAL\s+)?TABLE\b", normalized, re.IGNORECASE)
        assert not re.search(
            r"\b(?:DROP|TRUNCATE|DELETE|INSERT|UPDATE|ALTER)\b",
            normalized,
            re.IGNORECASE,
        )


def test_python_package_declares_cli_and_complete_dev_extra() -> None:
    document = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]
    assert project["name"] == "table-factory"
    assert ">=3.12" in project["requires-python"].replace(" ", "")

    scripts = project.get("scripts", {})
    assert "table-factory" in scripts
    assert scripts["table-factory"].endswith(":main")

    dev_requirements = project.get("optional-dependencies", {}).get("dev", [])
    normalized = {
        re.split(r"[\s<>=!~;\[]", requirement, maxsplit=1)[0].lower()
        for requirement in dev_requirements
    }
    assert {"pytest", "ruff", "mypy", "build"} <= normalized

    build_system = document.get("build-system", {})
    assert build_system.get("build-backend")
    assert build_system.get("requires")
