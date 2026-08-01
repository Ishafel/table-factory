"""Dialect-specific identifier and string-literal quoting."""

from __future__ import annotations


def hive_identifier(value: str) -> str:
    return f"`{value.replace('`', '``')}`"


def greenplum_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def hive_string(value: str) -> str:
    """Quote a string using Hive's ``escapeSQLString`` escape set."""
    escapes = {
        "\0": "\\0",
        "'": "\\'",
        '"': '\\"',
        "\b": "\\b",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\\": "\\\\",
        "\x1a": "\\Z",
    }
    escaped: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        next_character = value[index + 1] if index + 1 < len(value) else ""
        if character == "\\" and next_character in {"%", "_"}:
            escaped.extend((character, next_character))
            index += 2
            continue
        replacement = escapes.get(character)
        if replacement is not None:
            escaped.append(replacement)
        elif ord(character) < 32:
            escaped.append(f"\\u{ord(character):04x}")
        else:
            escaped.append(character)
        index += 1
    return "'" + "".join(escaped) + "'"


def greenplum_string(value: str) -> str:
    """Quote a PostgreSQL/Greenplum string independently of session settings."""
    if "\0" in value:
        raise ValueError("Greenplum string literals cannot contain NUL")
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"E'{escaped}'"


def hive_qualified(database: str | None, table: str) -> str:
    if database is None:
        return hive_identifier(table)
    return f"{hive_identifier(database)}.{hive_identifier(table)}"


def greenplum_qualified(schema: str, table: str) -> str:
    return f"{greenplum_identifier(schema)}.{greenplum_identifier(table)}"
