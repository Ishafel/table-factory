"""The single CLI used by both Docker and wheel installations."""

from __future__ import annotations

import argparse
import codecs
import json
import os
import stat
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from table_factory.config import load_config
from table_factory.errors import TableFactoryError
from table_factory.workflow import (
    display_path,
    generate,
    prepare,
    resolve_from_cwd,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="table-factory",
        description=(
            "Generate deterministic Hive-to-Greenplum transfer SQL from read-only Hive DDL."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="generate SQL artifacts")
    generate_parser.add_argument("--input", required=True, dest="input_path")
    generate_parser.add_argument("--output", required=True, dest="output_path")
    generate_parser.add_argument("--config", required=True, dest="config_path")

    validate_parser = subparsers.add_parser("validate", help="validate Hive DDL")
    validate_parser.add_argument("--input", required=True, dest="input_path")
    validate_parser.add_argument("--config", required=True, dest="config_path")

    inspect_parser = subparsers.add_parser("inspect", help="print the parsed model as JSON")
    inspect_parser.add_argument("ddl_path")
    inspect_parser.add_argument("--config", required=True, dest="config_path")
    return parser


def _uses_utf8(encoding: object) -> bool:
    if not isinstance(encoding, str):
        return False
    try:
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False


def _require_utf8_runtime() -> None:
    """Reject process encodings that cannot honor the CLI's Unicode contract."""
    encodings = (
        ("filesystem", sys.getfilesystemencoding()),
        ("standard output", getattr(sys.stdout, "encoding", None)),
        ("standard error", getattr(sys.stderr, "encoding", None)),
    )
    incompatible = [
        label for label, encoding in encodings if encoding is not None and not _uses_utf8(encoding)
    ]
    if incompatible:
        labels = ", ".join(incompatible)
        raise TableFactoryError(
            f"UTF-8 runtime is required; {labels} must use UTF-8; set PYTHONUTF8=1"
        )


def _load_configuration(config_value: str, *, cwd: Path) -> object:
    config_path = resolve_from_cwd(config_value, cwd=cwd)
    return load_config(
        config_path,
        display_name=display_path(config_path, cwd=cwd),
    )


def _run(arguments: argparse.Namespace, *, cwd: Path) -> int:
    config = _load_configuration(arguments.config_path, cwd=cwd)
    # load_config has one concrete return type. This assertion protects this
    # dispatch boundary while keeping argparse's dynamic Namespace contained.
    from table_factory.config import FactoryConfig

    if not isinstance(config, FactoryConfig):
        raise TableFactoryError("internal configuration error")

    if arguments.command == "generate":
        input_path = resolve_from_cwd(arguments.input_path, cwd=cwd)
        output_path = resolve_from_cwd(arguments.output_path, cwd=cwd)
        count = generate(
            input_path,
            output_path,
            config=config,
            cwd=cwd,
        )
        output_label = display_path(output_path, cwd=cwd)
        print(f"Generated {count} SQL files in {output_label}.")
        return 0

    if arguments.command == "validate":
        input_path = resolve_from_cwd(arguments.input_path, cwd=cwd)
        prepared = prepare(input_path, config=config, cwd=cwd)
        print(f"Validated {len(prepared.plans)} table(s) in {len(prepared.parsed_files)} file(s).")
        return 0

    if arguments.command == "inspect":
        ddl_path = resolve_from_cwd(arguments.ddl_path, cwd=cwd)
        try:
            ddl_status = ddl_path.stat()
        except FileNotFoundError:
            raise TableFactoryError("inspect requires one SQL file") from None
        except OSError as error:
            label = display_path(ddl_path, cwd=cwd)
            detail = error.strerror or "I/O error"
            raise TableFactoryError(f"cannot inspect input {label}: {detail}") from None
        except ValueError:
            label = display_path(ddl_path, cwd=cwd)
            raise TableFactoryError(f"cannot inspect input {label}: invalid path") from None
        if not stat.S_ISREG(ddl_status.st_mode):
            raise TableFactoryError("inspect requires one SQL file")
        prepared = prepare(ddl_path, config=config, cwd=cwd)
        payload = {
            "source": prepared.parsed_files[0].label,
            "config": config.as_dict(),
            "tables": [plan.as_dict() for plan in prepared.plans],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    raise TableFactoryError("unknown command")


def _current_working_directory() -> Path:
    try:
        return Path.cwd()
    except OSError:
        raise TableFactoryError("current working directory is unavailable") from None


def _silence_broken_stdout() -> None:
    """Prevent interpreter shutdown from flushing a closed pipe again."""
    try:
        stdout_descriptor = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        null_descriptor = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(null_descriptor, stdout_descriptor)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            os.close(null_descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = _parser()
    try:
        _require_utf8_runtime()
        arguments = parser.parse_args(argv)
        exit_code = _run(arguments, cwd=_current_working_directory())
        sys.stdout.flush()
        return exit_code
    except BrokenPipeError:
        _silence_broken_stdout()
        return 0
    except TableFactoryError as error:
        print(f"table-factory: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
