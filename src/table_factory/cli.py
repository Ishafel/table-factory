"""The single CLI used by both Docker and wheel installations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from table_factory.config import load_config
from table_factory.errors import TableFactoryError
from table_factory.workflow import (
    display_path,
    generate,
    parse_files,
    resolve_from_cwd,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="table-factory",
        description="Generate deterministic SQL artifacts from Hive DDL.",
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
        parsed = parse_files(input_path, cwd=cwd)
        table_count = sum(len(document.tables) for document in parsed)
        print(f"Validated {table_count} table(s) in {len(parsed)} file(s).")
        return 0

    if arguments.command == "inspect":
        ddl_path = resolve_from_cwd(arguments.ddl_path, cwd=cwd)
        if not ddl_path.is_file():
            raise TableFactoryError("inspect requires one SQL file")
        parsed = parse_files(ddl_path, cwd=cwd)
        document = parsed[0]
        payload = {
            "source": document.label,
            "config": config.as_dict(),
            "tables": [table.as_dict() for table in document.tables],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    raise TableFactoryError("unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        return _run(arguments, cwd=Path.cwd())
    except TableFactoryError as error:
        print(f"table-factory: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
