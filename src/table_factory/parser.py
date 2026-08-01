"""A deliberately small parser for Hive CREATE TABLE DDL."""

from __future__ import annotations

import re
from collections.abc import Iterator

from table_factory.errors import DdlParseError
from table_factory.models import Column, Table

_IDENTIFIER = r"(?:`(?:``|[^`])+`|[A-Za-z_][A-Za-z0-9_]*)"
_CREATE_TABLE = re.compile(
    rf"""
    \bCREATE\s+
    (?:TEMPORARY\s+)?
    (?P<external>EXTERNAL\s+)?
    TABLE\s+
    (?:IF\s+NOT\s+EXISTS\s+)?
    (?P<name>{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?)
    \s*\(
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)
_COLUMN = re.compile(rf"^\s*(?P<name>{_IDENTIFIER})\s+(?P<type>.+?)\s*$", re.DOTALL)
_NON_COLUMN_PREFIXES = {
    "CONSTRAINT",
    "PRIMARY",
    "FOREIGN",
    "UNIQUE",
    "CHECK",
}
_SIMPLE_TYPES = {
    "BIGINT",
    "BINARY",
    "BOOLEAN",
    "DATE",
    "FLOAT",
    "INT",
    "INTEGER",
    "INTERVAL_DAY_TIME",
    "INTERVAL_YEAR_MONTH",
    "SMALLINT",
    "STRING",
    "TIMESTAMP",
    "TIMESTAMPLOCALTZ",
    "TIMESTAMP_LTZ",
    "TINYINT",
    "VOID",
}
_COLUMN_CLAUSE_KEYWORDS = {
    "CHECK",
    "COMMENT",
    "DEFAULT",
    "NOT",
    "PRIMARY",
    "REFERENCES",
    "UNIQUE",
}
_CONSTRAINT_MODIFIERS = r"(?:\s+(?:ENABLE|DISABLE|VALIDATE|NOVALIDATE|RELY|NORELY))*"
_HIVE_STRING_PATTERN = r"(?:'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\")"
_TABLE_CLAUSE_ORDER = {
    "COMMENT": 0,
    "PARTITIONED": 1,
    "CLUSTERED": 2,
    "SKEWED": 3,
    "ROW": 4,
    "STORED": 5,
    "LOCATION": 6,
    "TBLPROPERTIES": 7,
}
_MAX_TYPE_PARAMETER_DIGITS = 32
_MAX_TYPE_NESTING = 100


def _without_comments(sql: str) -> str:
    """Mask SQL comments without changing offsets into the original document."""
    result = list(sql)
    index = 0
    quote: str | None = None
    while index < len(sql):
        character = sql[index]
        next_character = sql[index + 1] if index + 1 < len(sql) else ""

        if quote is not None:
            if character == quote:
                if next_character == quote and quote in {"'", '"', "`"}:
                    index += 2
                    continue
                quote = None
            elif character == "\\" and next_character:
                index += 2
                continue
            index += 1
            continue

        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            continue
        if character == "-" and next_character == "-":
            result[index] = " "
            result[index + 1] = " "
            index += 2
            while index < len(sql) and sql[index] not in "\r\n":
                result[index] = " "
                index += 1
            continue
        if character == "/" and next_character == "*":
            closing = sql.find("*/", index + 2)
            if closing < 0:
                raise DdlParseError("unterminated block comment")
            for comment_index in range(index, closing + 2):
                if sql[comment_index] not in "\r\n":
                    result[comment_index] = " "
            index = closing + 2
            continue

        index += 1

    if quote is not None:
        raise DdlParseError("unterminated quoted value")
    return "".join(result)


def _closing_parenthesis(sql: str, opening: int) -> int:
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(sql):
        character = sql[index]
        next_character = sql[index + 1] if index + 1 < len(sql) else ""
        if quote is not None:
            if character == quote:
                if next_character == quote:
                    index += 2
                    continue
                quote = None
            elif character == "\\" and next_character:
                index += 2
                continue
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise DdlParseError("unclosed CREATE TABLE column list")


def _quoted_character_mask(sql: str) -> list[bool]:
    """Mark characters inside any SQL quote so keywords there are ignored."""
    quoted = [False] * len(sql)
    index = 0
    quote: str | None = None
    while index < len(sql):
        character = sql[index]
        next_character = sql[index + 1] if index + 1 < len(sql) else ""
        if quote is not None:
            quoted[index] = True
            if character == quote:
                if next_character == quote:
                    quoted[index + 1] = True
                    index += 2
                    continue
                quote = None
            elif character == "\\" and next_character:
                quoted[index + 1] = True
                index += 2
                continue
        elif character in {"'", '"', "`"}:
            quoted[index] = True
            quote = character
        index += 1
    return quoted


def _top_level_parts(value: str, *, delimiter: str = ",") -> Iterator[str]:
    start = 0
    round_depth = 0
    angle_depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        next_character = value[index + 1] if index + 1 < len(value) else ""
        if quote is not None:
            if character == quote:
                if next_character == quote:
                    index += 2
                    continue
                quote = None
            elif character == "\\" and next_character:
                index += 2
                continue
        elif character in {"'", '"', "`"}:
            quote = character
        elif character == "(":
            round_depth += 1
        elif character == ")":
            round_depth -= 1
            if round_depth < 0:
                raise DdlParseError("unbalanced parentheses in a column definition")
        elif character == "<" and round_depth == 0:
            angle_depth += 1
        elif character == ">" and round_depth == 0 and angle_depth > 0:
            angle_depth -= 1
        elif character == delimiter and round_depth == 0 and angle_depth == 0:
            yield value[start:index]
            start = index + 1
        index += 1
    if quote is not None or round_depth != 0 or angle_depth != 0:
        raise DdlParseError("unbalanced Hive type in a column definition")
    yield value[start:]


def _split_column_clauses(type_and_comment: str) -> tuple[str, str | None]:
    """Separate a Hive type from supported top-level column clauses."""
    round_depth = 0
    angle_depth = 0
    quote: str | None = None
    index = 0
    while index < len(type_and_comment):
        character = type_and_comment[index]
        next_character = type_and_comment[index + 1] if index + 1 < len(type_and_comment) else ""
        if quote is not None:
            if character == quote:
                if next_character == quote:
                    index += 2
                    continue
                quote = None
            elif character == "\\" and next_character:
                index += 2
                continue
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "(":
            round_depth += 1
        elif character == ")":
            round_depth -= 1
        elif character == "<":
            angle_depth += 1
        elif character == ">":
            angle_depth -= 1
        elif round_depth == 0 and angle_depth == 0:
            keyword_match = re.match(
                r"[A-Za-z_]+",
                type_and_comment[index:],
            )
            keyword = keyword_match.group(0).upper() if keyword_match else ""
            before = type_and_comment[index - 1] if index else " "
            if keyword in _COLUMN_CLAUSE_KEYWORDS and not (before.isalnum() or before == "_"):
                clauses = type_and_comment[index:].strip()
                return (
                    type_and_comment[:index].strip(),
                    _parse_column_clauses(clauses),
                )
        index += 1
    return type_and_comment.strip(), None


def _quoted_literal_end(value: str, start: int = 0) -> int:
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    if index >= len(value) or value[index] not in {"'", '"'}:
        raise DdlParseError("expected a quoted Hive string")
    quote = value[index]
    index += 1
    while index < len(value):
        character = value[index]
        next_character = value[index + 1] if index + 1 < len(value) else ""
        if character == quote:
            if next_character == quote:
                index += 2
                continue
            return index + 1
        if character == "\\" and next_character:
            index += 2
            continue
        index += 1
    raise DdlParseError("unterminated quoted Hive string")


def _quoted_literal(value: str, start: int = 0) -> tuple[str, int]:
    """Decode one Hive quoted string and return its end offset."""
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    end = _quoted_literal_end(value, start)
    quote = value[index]
    cursor = index + 1
    content_end = end - 1
    decoded: list[str] = []
    escapes = {
        "0": "\0",
        "b": "\b",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "Z": "\x1a",
    }
    while cursor < content_end:
        character = value[cursor]
        next_character = value[cursor + 1] if cursor + 1 < content_end else ""
        if character == quote and next_character == quote:
            decoded.append(quote)
            cursor += 2
            continue
        if character == "\\" and next_character:
            if next_character == "u" and cursor + 6 <= content_end:
                hexadecimal = value[cursor + 2 : cursor + 6]
                if re.fullmatch(r"[0-9A-Fa-f]{4}", hexadecimal) is None:
                    raise DdlParseError("invalid Unicode escape in a Hive string")
                decoded.append(chr(int(hexadecimal, 16)))
                cursor += 6
                continue
            octal = value[cursor + 1 : cursor + 4]
            if (
                len(octal) == 3
                and octal[0] in "01"
                and all(digit in "01234567" for digit in octal[1:])
            ):
                decoded.append(chr(int(octal, 8)))
                cursor += 4
                continue
            if next_character in {"%", "_"}:
                decoded.append(f"\\{next_character}")
            else:
                decoded.append(escapes.get(next_character, next_character))
            cursor += 2
            continue
        decoded.append(character)
        cursor += 1
    try:
        result = "".join(decoded).encode("utf-16-le", errors="surrogatepass").decode("utf-16-le")
    except UnicodeError:
        raise DdlParseError("invalid Unicode surrogate escape in a Hive string") from None
    return result, end


def _balanced_expression_end(
    value: str,
    start: int,
    *,
    label: str = "CHECK",
) -> int:
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    if index >= len(value) or value[index] != "(":
        raise DdlParseError(f"{label} must contain a parenthesized expression")
    depth = 0
    quote: str | None = None
    while index < len(value):
        character = value[index]
        next_character = value[index + 1] if index + 1 < len(value) else ""
        if quote is not None:
            if character == quote:
                if next_character == quote:
                    index += 2
                    continue
                quote = None
            elif character == "\\" and next_character:
                index += 2
                continue
        elif character in {"'", '"'}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise DdlParseError(f"unclosed {label} expression")


def _parenthesized_contents(
    value: str,
    start: int,
    *,
    label: str,
) -> tuple[str, int]:
    opening = start
    while opening < len(value) and value[opening].isspace():
        opening += 1
    end = _balanced_expression_end(value, start, label=label)
    return value[opening + 1 : end - 1], end


def _validate_properties(value: str, *, label: str) -> None:
    parts = tuple(_top_level_parts(value))
    if not parts or any(
        re.fullmatch(
            rf"\s*{_HIVE_STRING_PATTERN}\s*=\s*{_HIVE_STRING_PATTERN}\s*",
            part,
            flags=re.DOTALL,
        )
        is None
        for part in parts
    ):
        raise DdlParseError(f"{label} must contain quoted key/value pairs")


def _parse_column_clauses(value: str) -> str | None:
    """Validate column clauses and return their semantic Hive comment."""
    remaining = value.strip()
    comment: str | None = None
    while remaining:
        upper = remaining.upper()
        if re.match(r"COMMENT\b", upper):
            if comment is not None:
                raise DdlParseError("duplicate column COMMENT clause")
            comment, end = _quoted_literal(remaining, len("COMMENT"))
            remaining = remaining[end:].strip()
            continue
        match = re.match(
            rf"NOT\s+NULL{_CONSTRAINT_MODIFIERS}",
            remaining,
            flags=re.IGNORECASE,
        )
        if match:
            remaining = remaining[match.end() :].strip()
            continue
        match = re.match(
            rf"(?:PRIMARY\s+KEY|UNIQUE){_CONSTRAINT_MODIFIERS}",
            remaining,
            flags=re.IGNORECASE,
        )
        if match:
            remaining = remaining[match.end() :].strip()
            continue
        if re.match(r"DEFAULT\b", upper):
            default_value = remaining[len("DEFAULT") :].lstrip()
            if not default_value:
                raise DdlParseError("DEFAULT must contain a value")
            if default_value[0] in {"'", '"'}:
                end = _quoted_literal_end(default_value)
            elif default_value[0] == "(":
                end = _balanced_expression_end(default_value, 0)
            else:
                atom = re.match(r"[^\s]+", default_value)
                if atom is None:
                    raise DdlParseError("DEFAULT must contain a value")
                end = atom.end()
            remaining = default_value[end:].strip()
            continue
        if re.match(r"CHECK\b", upper):
            end = _balanced_expression_end(remaining, len("CHECK"))
            remaining = remaining[end:].strip()
            modifier = re.match(
                r"(?:(?:ENABLE|DISABLE|VALIDATE|NOVALIDATE|RELY|NORELY)\s*)*",
                remaining,
                flags=re.IGNORECASE,
            )
            if modifier:
                remaining = remaining[modifier.end() :].strip()
            continue
        if re.match(r"REFERENCES\b", upper):
            match = re.match(
                rf"REFERENCES\s+{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?"
                rf"(?:\s*\(\s*{_IDENTIFIER}\s*\))?{_CONSTRAINT_MODIFIERS}",
                remaining,
                flags=re.IGNORECASE,
            )
            if match is None:
                raise DdlParseError("malformed REFERENCES clause")
            remaining = remaining[match.end() :].strip()
            continue
        raise DdlParseError("unsupported or malformed column constraint")
    return comment


def _consume_keyword(value: str, pattern: str, *, label: str) -> int:
    match = re.match(pattern, value, flags=re.IGNORECASE)
    if match is None:
        raise DdlParseError(f"malformed {label} clause")
    return match.end()


def _record_table_clause(
    clause: str,
    *,
    seen: set[str],
    previous_order: int,
) -> int:
    """Reject repeated or out-of-order Hive table clauses."""
    if clause in seen:
        raise DdlParseError(f"duplicate {clause} clause")
    order = _TABLE_CLAUSE_ORDER[clause]
    if order < previous_order:
        raise DdlParseError(f"{clause} clause is out of order")
    seen.add(clause)
    return order


def _parse_table_tail(value: str) -> tuple[tuple[Column, ...], str | None]:
    """Validate table clauses and return partition columns and table comment."""
    remaining = value.strip()
    seen: set[str] = set()
    previous_order = -1
    partition_columns: tuple[Column, ...] = ()
    table_comment: str | None = None
    while remaining:
        upper = remaining.upper()
        if re.match(r"COMMENT\b", upper):
            previous_order = _record_table_clause(
                "COMMENT",
                seen=seen,
                previous_order=previous_order,
            )
            table_comment, end = _quoted_literal(remaining, len("COMMENT"))
            remaining = remaining[end:].strip()
            continue
        if re.match(r"PARTITIONED\b", upper):
            previous_order = _record_table_clause(
                "PARTITIONED",
                seen=seen,
                previous_order=previous_order,
            )
            start = _consume_keyword(
                remaining,
                r"PARTITIONED\s+BY",
                label="PARTITIONED BY",
            )
            contents, end = _parenthesized_contents(
                remaining,
                start,
                label="PARTITIONED BY",
            )
            partition_columns = _columns(contents, allow_constraints=False)
            remaining = remaining[end:].strip()
            continue
        if re.match(r"CLUSTERED\b", upper):
            previous_order = _record_table_clause(
                "CLUSTERED",
                seen=seen,
                previous_order=previous_order,
            )
            start = _consume_keyword(
                remaining,
                r"CLUSTERED\s+BY",
                label="CLUSTERED BY",
            )
            end = _balanced_expression_end(
                remaining,
                start,
                label="CLUSTERED BY",
            )
            remaining = remaining[end:].strip()
            if re.match(r"SORTED\b", remaining, flags=re.IGNORECASE):
                start = _consume_keyword(
                    remaining,
                    r"SORTED\s+BY",
                    label="SORTED BY",
                )
                end = _balanced_expression_end(
                    remaining,
                    start,
                    label="SORTED BY",
                )
                remaining = remaining[end:].strip()
            end = _consume_keyword(
                remaining,
                r"INTO\s+\d+\s+BUCKETS\b",
                label="CLUSTERED BY",
            )
            remaining = remaining[end:].strip()
            continue
        if re.match(r"SKEWED\b", upper):
            previous_order = _record_table_clause(
                "SKEWED",
                seen=seen,
                previous_order=previous_order,
            )
            start = _consume_keyword(
                remaining,
                r"SKEWED\s+BY",
                label="SKEWED BY",
            )
            end = _balanced_expression_end(remaining, start, label="SKEWED BY")
            remaining = remaining[end:].strip()
            start = _consume_keyword(remaining, r"ON\b", label="SKEWED BY")
            end = _balanced_expression_end(remaining, start, label="SKEWED BY")
            remaining = remaining[end:].strip()
            match = re.match(
                r"STORED\s+AS\s+DIRECTORIES\b",
                remaining,
                flags=re.IGNORECASE,
            )
            if match:
                remaining = remaining[match.end() :].strip()
            continue
        if re.match(r"ROW\b", upper):
            previous_order = _record_table_clause(
                "ROW",
                seen=seen,
                previous_order=previous_order,
            )
            start = _consume_keyword(
                remaining,
                r"ROW\s+FORMAT",
                label="ROW FORMAT",
            )
            remaining = remaining[start:].strip()
            if re.match(r"SERDE\b", remaining, flags=re.IGNORECASE):
                end = _quoted_literal_end(remaining, len("SERDE"))
                remaining = remaining[end:].strip()
                match = re.match(
                    r"WITH\s+SERDEPROPERTIES",
                    remaining,
                    flags=re.IGNORECASE,
                )
                if match:
                    contents, end = _parenthesized_contents(
                        remaining,
                        match.end(),
                        label="SERDEPROPERTIES",
                    )
                    _validate_properties(contents, label="SERDEPROPERTIES")
                    remaining = remaining[end:].strip()
                continue
            start = _consume_keyword(
                remaining,
                r"DELIMITED\b",
                label="ROW FORMAT",
            )
            remaining = remaining[start:].strip()
            row_patterns = (
                r"FIELDS\s+TERMINATED\s+BY",
                r"ESCAPED\s+BY",
                r"COLLECTION\s+ITEMS\s+TERMINATED\s+BY",
                r"MAP\s+KEYS\s+TERMINATED\s+BY",
                r"LINES\s+TERMINATED\s+BY",
                r"NULL\s+DEFINED\s+AS",
            )
            while remaining and not re.match(
                (
                    r"(?:COMMENT|PARTITIONED|CLUSTERED|SKEWED|ROW|STORED|"
                    r"LOCATION|TBLPROPERTIES|AS)\b"
                ),
                remaining,
                flags=re.IGNORECASE,
            ):
                for pattern in row_patterns:
                    match = re.match(pattern, remaining, flags=re.IGNORECASE)
                    if match:
                        end = _quoted_literal_end(remaining, match.end())
                        remaining = remaining[end:].strip()
                        break
                else:
                    raise DdlParseError("malformed ROW FORMAT DELIMITED clause")
            continue
        if re.match(r"STORED\b", upper):
            previous_order = _record_table_clause(
                "STORED",
                seen=seen,
                previous_order=previous_order,
            )
            if re.match(r"STORED\s+BY\b", remaining, flags=re.IGNORECASE):
                start = _consume_keyword(
                    remaining,
                    r"STORED\s+BY",
                    label="STORED BY",
                )
                end = _quoted_literal_end(remaining, start)
                remaining = remaining[end:].strip()
                match = re.match(
                    r"WITH\s+SERDEPROPERTIES",
                    remaining,
                    flags=re.IGNORECASE,
                )
                if match:
                    contents, end = _parenthesized_contents(
                        remaining,
                        match.end(),
                        label="SERDEPROPERTIES",
                    )
                    _validate_properties(contents, label="SERDEPROPERTIES")
                    remaining = remaining[end:].strip()
                continue
            start = _consume_keyword(
                remaining,
                r"STORED\s+AS",
                label="STORED AS",
            )
            remaining = remaining[start:].strip()
            if re.match(r"INPUTFORMAT\b", remaining, flags=re.IGNORECASE):
                start = _consume_keyword(
                    remaining,
                    r"INPUTFORMAT",
                    label="INPUTFORMAT",
                )
                end = _quoted_literal_end(remaining, start)
                remaining = remaining[end:].strip()
                start = _consume_keyword(
                    remaining,
                    r"OUTPUTFORMAT",
                    label="OUTPUTFORMAT",
                )
                end = _quoted_literal_end(remaining, start)
                remaining = remaining[end:].strip()
                continue
            match = re.match(
                r"(?:TEXTFILE|SEQUENCEFILE|RCFILE|ORC|PARQUET|AVRO|JSONFILE)\b",
                remaining,
                flags=re.IGNORECASE,
            )
            if match is None:
                raise DdlParseError("unsupported or malformed STORED AS format")
            remaining = remaining[match.end() :].strip()
            continue
        if re.match(r"LOCATION\b", upper):
            previous_order = _record_table_clause(
                "LOCATION",
                seen=seen,
                previous_order=previous_order,
            )
            end = _quoted_literal_end(remaining, len("LOCATION"))
            remaining = remaining[end:].strip()
            continue
        if re.match(r"TBLPROPERTIES\b", upper):
            previous_order = _record_table_clause(
                "TBLPROPERTIES",
                seen=seen,
                previous_order=previous_order,
            )
            contents, end = _parenthesized_contents(
                remaining,
                len("TBLPROPERTIES"),
                label="TBLPROPERTIES",
            )
            _validate_properties(contents, label="TBLPROPERTIES")
            remaining = remaining[end:].strip()
            continue
        if re.match(r"AS\b", upper):
            raise DdlParseError("CREATE TABLE AS SELECT is unsupported")
        raise DdlParseError("unsupported or malformed Hive table clause")
    return partition_columns, table_comment


def _validate_table_constraint(definition: str) -> None:
    modifiers = _CONSTRAINT_MODIFIERS
    identifier_list = rf"\(\s*{_IDENTIFIER}(?:\s*,\s*{_IDENTIFIER})*\s*\)"
    constraint_prefix = rf"(?:CONSTRAINT\s+{_IDENTIFIER}\s+)?"
    patterns = (
        rf"{constraint_prefix}PRIMARY\s+KEY\s*{identifier_list}{modifiers}",
        rf"{constraint_prefix}UNIQUE\s*{identifier_list}{modifiers}",
        rf"{constraint_prefix}FOREIGN\s+KEY\s*{identifier_list}\s+"
        rf"REFERENCES\s+{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?\s*"
        rf"{identifier_list}{modifiers}",
        rf"{constraint_prefix}CHECK\s*\(.+\){modifiers}",
    )
    if not any(
        re.fullmatch(pattern, definition, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns
    ):
        raise DdlParseError("unsupported or malformed table constraint")


def _statement_end(sql: str, start: int) -> int:
    quote: str | None = None
    index = start
    while index < len(sql):
        character = sql[index]
        next_character = sql[index + 1] if index + 1 < len(sql) else ""
        if quote is not None:
            if character == quote:
                if next_character == quote:
                    index += 2
                    continue
                quote = None
            elif character == "\\" and next_character:
                index += 2
                continue
        elif character in {"'", '"', "`"}:
            quote = character
        elif character == ";":
            return index + 1
        index += 1
    return len(sql)


def _unquote(identifier: str) -> str:
    stripped = identifier.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == "`":
        return stripped[1:-1].replace("``", "`")
    return stripped


def _qualified_name(raw_name: str) -> tuple[str | None, str]:
    parts: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(raw_name):
        character = raw_name[index]
        next_character = raw_name[index + 1] if index + 1 < len(raw_name) else ""
        if quote is not None:
            if character == quote:
                if next_character == quote:
                    index += 2
                    continue
                quote = None
        elif character == "`":
            quote = character
        elif character == ".":
            parts.append(raw_name[start:index].strip())
            start = index + 1
        index += 1
    parts.append(raw_name[start:].strip())
    if len(parts) == 1:
        raw_part = parts[0]
        name = _unquote(raw_part)
        if not (len(raw_part) >= 2 and raw_part[0] == raw_part[-1] == "`"):
            return None, name
        embedded_parts = name.split(".")
        if len(embedded_parts) == 1:
            return None, name
        if len(embedded_parts) == 2:
            database, table = embedded_parts
            if not database or not table:
                raise DdlParseError(
                    "a quoted qualified table name must contain both database and table"
                )
            return database, table
        raise DdlParseError("table names may contain at most one database qualifier")
    if len(parts) == 2:
        return _unquote(parts[0]), _unquote(parts[1])
    raise DdlParseError("table names may contain at most one database qualifier")


class _TypeParser:
    """Recursive-descent validator for the common Hive type grammar."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.index = 0

    def parse(self) -> None:
        self._type(depth=0)
        self._whitespace()
        if self.index != len(self.value):
            raise DdlParseError("unsupported or malformed Hive data type")

    def _whitespace(self) -> None:
        while self.index < len(self.value) and self.value[self.index].isspace():
            self.index += 1

    def _word(self) -> str:
        self._whitespace()
        match = re.match(r"[A-Za-z_][A-Za-z0-9_$-]*", self.value[self.index :])
        if match is None:
            raise DdlParseError("expected a Hive type name")
        self.index += len(match.group(0))
        return match.group(0).upper()

    def _consume(self, token: str) -> bool:
        self._whitespace()
        if self.value.startswith(token, self.index):
            self.index += len(token)
            return True
        return False

    def _expect(self, token: str) -> None:
        if not self._consume(token):
            raise DdlParseError(f"expected '{token}' in a Hive data type")

    def _integer(self) -> int:
        self._whitespace()
        match = re.match(r"[0-9]+", self.value[self.index :])
        if match is None:
            raise DdlParseError("expected an integer type parameter")
        token = match.group(0)
        self.index += len(token)
        if len(token) > _MAX_TYPE_PARAMETER_DIGITS:
            raise DdlParseError("Hive integer type parameter exceeds the supported length")
        return int(token)

    def _field_name(self) -> None:
        self._whitespace()
        if self.index >= len(self.value):
            raise DdlParseError("expected a STRUCT field name")
        quote = self.value[self.index]
        if quote == "`":
            self.index += 1
            while self.index < len(self.value):
                character = self.value[self.index]
                if character == quote:
                    if self.index + 1 < len(self.value) and self.value[self.index + 1] == quote:
                        self.index += 2
                        continue
                    self.index += 1
                    return
                self.index += 1
            raise DdlParseError("unterminated STRUCT field name")
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", self.value[self.index :])
        if match is None:
            raise DdlParseError("expected a STRUCT field name")
        self.index += len(match.group(0))

    def _optional_comment(self) -> None:
        starting_index = self.index
        try:
            word = self._word()
        except DdlParseError:
            self.index = starting_index
            return
        if word != "COMMENT":
            self.index = starting_index
            return
        self.index = _quoted_literal_end(self.value, self.index)

    def _type_list(self, *, depth: int) -> None:
        self._type(depth=depth)
        while self._consume(","):
            self._type(depth=depth)

    def _type(self, *, depth: int) -> bool:
        """Parse one type and report whether it is primitive."""
        if depth > _MAX_TYPE_NESTING:
            raise DdlParseError(
                f"Hive data type nesting exceeds the supported limit of {_MAX_TYPE_NESTING}"
            )
        name = self._word()
        if name in _SIMPLE_TYPES:
            if name == "TIMESTAMP" and self._remaining_words("WITH", "LOCAL", "TIME", "ZONE"):
                return True
            return True
        if name in {"DOUBLE", "REAL"}:
            self._remaining_words("PRECISION")
            return True
        if name in {"DECIMAL", "NUMERIC"}:
            if self._consume("("):
                precision = self._integer()
                scale = 0
                if self._consume(","):
                    scale = self._integer()
                self._expect(")")
                if precision <= 0 or precision > 38 or scale > precision:
                    raise DdlParseError("invalid DECIMAL precision or scale")
            return True
        if name in {"CHAR", "VARCHAR"}:
            self._expect("(")
            length = self._integer()
            self._expect(")")
            maximum = 255 if name == "CHAR" else 65_535
            if length <= 0 or length > maximum:
                raise DdlParseError(f"{name} length must be between 1 and {maximum}")
            return True
        if name == "ARRAY":
            self._expect("<")
            self._type(depth=depth + 1)
            self._expect(">")
            return False
        if name == "MAP":
            self._expect("<")
            if not self._type(depth=depth + 1):
                raise DdlParseError("MAP keys must use a primitive Hive type")
            self._expect(",")
            self._type(depth=depth + 1)
            self._expect(">")
            return False
        if name == "UNIONTYPE":
            self._expect("<")
            self._type_list(depth=depth + 1)
            self._expect(">")
            return False
        if name == "STRUCT":
            self._expect("<")
            self._field_name()
            self._expect(":")
            self._type(depth=depth + 1)
            self._optional_comment()
            while self._consume(","):
                self._field_name()
                self._expect(":")
                self._type(depth=depth + 1)
                self._optional_comment()
            self._expect(">")
            return False
        # A syntactically simple custom type is preserved for the semantic
        # type mapper, which can then report table/column/type context. Any
        # trailing parameters or tokens are still rejected by ``parse``.
        return True

    def _remaining_words(self, *words: str) -> bool:
        starting_index = self.index
        for expected in words:
            try:
                actual = self._word()
            except DdlParseError:
                self.index = starting_index
                return False
            if actual != expected:
                self.index = starting_index
                return False
        return True


def _validate_type(data_type: str) -> None:
    _TypeParser(data_type).parse()


def _columns(
    column_list: str,
    *,
    allow_constraints: bool = True,
) -> tuple[Column, ...]:
    columns: list[Column] = []
    for raw_definition in _top_level_parts(column_list):
        definition = raw_definition.strip()
        if not definition:
            raise DdlParseError("empty column definition")
        first_word = definition.split(maxsplit=1)[0].upper()
        if first_word in _NON_COLUMN_PREFIXES:
            if not allow_constraints:
                raise DdlParseError("PARTITIONED BY must contain only column definitions")
            _validate_table_constraint(definition)
            continue
        match = _COLUMN.match(definition)
        if match is None:
            raise DdlParseError("cannot parse a column definition")
        data_type, comment = _split_column_clauses(match.group("type"))
        if not data_type:
            raise DdlParseError("a column is missing its data type")
        _validate_type(data_type)
        columns.append(
            Column(
                name=_unquote(match.group("name")),
                data_type=data_type,
                comment=comment,
            )
        )
    if not columns:
        raise DdlParseError("CREATE TABLE must define at least one column")
    return tuple(columns)


def parse_hive_ddl(sql: str) -> tuple[Table, ...]:
    """Parse all CREATE TABLE statements in a UTF-8 SQL document."""
    cleaned = _without_comments(sql)
    quoted = _quoted_character_mask(cleaned)
    matches = [match for match in _CREATE_TABLE.finditer(cleaned) if not quoted[match.start()]]
    if not matches:
        raise DdlParseError("no Hive CREATE TABLE statement found")

    tables: list[Table] = []
    cursor = 0
    for match in matches:
        prefix = cleaned[cursor : match.start()].strip(" \t\r\n;")
        if prefix:
            raise DdlParseError("only CREATE TABLE statements are supported")
        opening = match.end() - 1
        closing = _closing_parenthesis(cleaned, opening)
        statement_end = _statement_end(cleaned, closing + 1)
        tail_end = (
            statement_end - 1
            if cleaned[statement_end - 1 : statement_end] == ";"
            else statement_end
        )
        partition_columns, table_comment = _parse_table_tail(cleaned[closing + 1 : tail_end])
        database, name = _qualified_name(match.group("name"))
        tables.append(
            Table(
                database=database,
                name=name,
                columns=_columns(cleaned[opening + 1 : closing]),
                partition_columns=partition_columns,
                external=match.group("external") is not None,
                comment=table_comment,
                create_sql=sql[match.start() : statement_end].strip(),
            )
        )
        cursor = statement_end
    if cleaned[cursor:].strip(" \t\r\n;"):
        raise DdlParseError("only CREATE TABLE statements are supported")
    return tuple(tables)
