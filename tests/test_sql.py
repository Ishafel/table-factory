from __future__ import annotations

import pytest

from table_factory.config import FactoryConfig
from table_factory.generator import render_artifacts
from table_factory.parser import parse_hive_ddl
from table_factory.sql import greenplum_string


def _decode_greenplum_string(
    literal: str,
    *,
    standard_conforming_strings: bool,
) -> tuple[str, int]:
    """Decode the quote/backslash subset of PostgreSQL escape-string syntax."""
    escape_string = literal.startswith("E'")
    index = 2 if escape_string else 1
    assert literal[index - 1] == "'"
    backslash_escapes = escape_string or not standard_conforming_strings
    decoded: list[str] = []
    escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "\\": "\\",
        "'": "'",
    }
    while index < len(literal):
        character = literal[index]
        next_character = literal[index + 1] if index + 1 < len(literal) else ""
        if character == "'":
            if next_character == "'":
                decoded.append("'")
                index += 2
                continue
            return "".join(decoded), index + 1
        if character == "\\" and backslash_escapes:
            assert next_character
            decoded.append(escapes.get(next_character, next_character))
            index += 2
            continue
        decoded.append(character)
        index += 1
    raise AssertionError("unterminated PostgreSQL string literal")


def test_greenplum_string_escapes_an_injection_payload() -> None:
    value = r"x\'; DROP TABLE victim; --"

    assert greenplum_string(value) == r"E'x\\''; DROP TABLE victim; --'"


@pytest.mark.parametrize(
    "standard_conforming_strings",
    (True, False),
    ids=("on", "off"),
)
def test_greenplum_string_round_trips_with_both_session_settings(
    standard_conforming_strings: bool,
) -> None:
    value = r"x\'; DROP TABLE victim; --"
    literal = greenplum_string(value)

    decoded, end = _decode_greenplum_string(
        literal,
        standard_conforming_strings=standard_conforming_strings,
    )

    assert decoded == value
    assert end == len(literal)


def test_reference_lexer_exposes_the_previous_off_setting_breakout() -> None:
    legacy_literal = r"'x\''; DROP TABLE victim; --'"

    _, end = _decode_greenplum_string(
        legacy_literal,
        standard_conforming_strings=False,
    )

    assert legacy_literal[end:].startswith("; DROP TABLE victim;")


def test_greenplum_string_always_uses_an_explicit_escape_literal() -> None:
    assert greenplum_string("O'Brien") == "E'O''Brien'"


def test_greenplum_string_rejects_nul() -> None:
    with pytest.raises(ValueError, match="cannot contain NUL"):
        greenplum_string("before\0after")


def test_greenplum_comment_rendering_quotes_the_injection_payload() -> None:
    ddl = (
        r"CREATE TABLE source_db.events ("
        r"value STRING COMMENT 'x\\\'; DROP TABLE victim; --'"
        r") COMMENT 'x\\\'; DROP TABLE victim; --';"
    )
    table = parse_hive_ddl(ddl)[0]
    artifacts = {
        artifact.filename: artifact.content
        for artifact in render_artifacts(
            table,
            config=FactoryConfig(),
            source_label="source.sql",
        )
    }
    safe_literal = r"E'x\\''; DROP TABLE victim; --'"

    assert table.comment == r"x\'; DROP TABLE victim; --"
    assert table.columns[0].comment == r"x\'; DROP TABLE victim; --"
    for role, schema, target in (
        ("03_greenplum_create_external", "ext", "replica_events_ext"),
        ("04_greenplum_create_physical", "dwh", "replica_events"),
    ):
        content = artifacts[f"source_db_events__{role}.sql"]
        assert f'COMMENT ON TABLE "{schema}"."{target}" IS {safe_literal};' in content
        assert f'COMMENT ON COLUMN "{schema}"."{target}"."value" IS {safe_literal};' in content
