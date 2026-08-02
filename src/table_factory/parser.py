"""A deliberately small parser for Hive CREATE TABLE DDL."""

from __future__ import annotations

import re
import unicodedata
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
    "CONSTRAINT",
    "DEFAULT",
    "NOT",
    "PRIMARY",
    "REFERENCES",
    "UNIQUE",
}
_HIVE_STRING_PATTERN = r"(?:'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\")"
_HIVE_NUMERIC_LITERAL = re.compile(
    r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?)"
    r"(?:BD|[YSLFD])?",
    flags=re.IGNORECASE,
)
_GENERIC_CONSTRAINT_OPTIONS = re.compile(
    r"(?:"
    r"ENABLE(?:\s+(?:VALIDATE|NOVALIDATE))?"
    r"|DISABLE(?:\s+(?:VALIDATE|NOVALIDATE))?"
    r"|ENFORCED"
    r"|NOT\s+ENFORCED"
    r")(?:\s+(?:RELY|NORELY))?\b",
    flags=re.IGNORECASE,
)
_DEFAULT_CONSTRAINT_OPTIONS = re.compile(
    r"(?:ENABLE|ENFORCED|DISABLE)\b",
    flags=re.IGNORECASE,
)
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
_MAX_DEFAULT_CAST_NESTING = 32

type _StructuralReference = tuple[str, tuple[str, ...]]


def _identifier_comparison(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _quoted_scan_step(
    value: str,
    index: int,
    quote: str,
) -> tuple[int, str | None]:
    """Advance inside one SQL quote and return the remaining quote state."""
    character = value[index]
    next_character = value[index + 1] if index + 1 < len(value) else ""
    if character == quote:
        if next_character == quote:
            return index + 2, quote
        return index + 1, None
    if quote in {"'", '"'} and character == "\\" and next_character:
        return index + 2, quote
    return index + 1, quote


def _without_comments(sql: str) -> str:
    """Mask SQL comments without changing offsets into the original document."""
    result = list(sql)
    index = 0
    quote: str | None = None
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            index, quote = _quoted_scan_step(sql, index, quote)
            continue

        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            continue
        next_character = sql[index + 1] if index + 1 < len(sql) else ""
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


def _matching_parenthesis_end(
    value: str,
    opening: int,
    *,
    label: str,
) -> int:
    """Return the exclusive end of a quote-aware parenthesized expression."""
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(value):
        character = value[index]
        if quote is not None:
            index, quote = _quoted_scan_step(value, index, quote)
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise DdlParseError(f"unclosed {label}")


def _closing_parenthesis(sql: str, opening: int) -> int:
    return (
        _matching_parenthesis_end(
            sql,
            opening,
            label="CREATE TABLE column list",
        )
        - 1
    )


def _quoted_character_mask(sql: str) -> list[bool]:
    """Mark characters inside any SQL quote so keywords there are ignored."""
    quoted = [False] * len(sql)
    index = 0
    quote: str | None = None
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            starting_index = index
            index, quote = _quoted_scan_step(sql, index, quote)
            for quoted_index in range(starting_index, index):
                quoted[quoted_index] = True
            continue
        if character in {"'", '"', "`"}:
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
        if quote is not None:
            index, quote = _quoted_scan_step(value, index, quote)
            continue
        if character in {"'", '"', "`"}:
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


def _split_column_clauses(
    type_and_comment: str,
    *,
    allow_constraints: bool = True,
) -> tuple[str, str | None]:
    """Separate a Hive type from supported top-level column clauses."""
    round_depth = 0
    angle_depth = 0
    quote: str | None = None
    index = 0
    while index < len(type_and_comment):
        character = type_and_comment[index]
        if quote is not None:
            index, quote = _quoted_scan_step(type_and_comment, index, quote)
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
                    _parse_column_clauses(
                        clauses,
                        allow_constraints=allow_constraints,
                    ),
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
        index, remaining_quote = _quoted_scan_step(value, index, quote)
        if remaining_quote is None:
            return index
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
    return _matching_parenthesis_end(
        value,
        index,
        label=f"{label} expression",
    )


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


def _identifier_list(value: str, *, label: str) -> tuple[str, ...]:
    parts = tuple(_top_level_parts(value))
    if not parts or any(
        re.fullmatch(rf"\s*{_IDENTIFIER}\s*", part, flags=re.DOTALL) is None for part in parts
    ):
        raise DdlParseError(f"{label} must contain only column identifiers")
    identifiers = tuple(_unquote(part) for part in parts)
    comparisons = tuple(_identifier_comparison(identifier) for identifier in identifiers)
    if len(set(comparisons)) != len(comparisons):
        raise DdlParseError(f"{label} must not repeat column identifiers")
    return identifiers


def _sort_list(value: str) -> tuple[str, ...]:
    parts = tuple(_top_level_parts(value))
    matches = tuple(
        re.fullmatch(
            rf"\s*(?P<name>{_IDENTIFIER})(?:\s+(?:ASC|DESC))?\s*",
            part,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for part in parts
    )
    if not parts or any(match is None for match in matches):
        raise DdlParseError(
            "SORTED BY must contain only column identifiers with optional ASC or DESC"
        )
    identifiers = tuple(_unquote(match.group("name")) for match in matches if match is not None)
    comparisons = tuple(_identifier_comparison(identifier) for identifier in identifiers)
    if len(set(comparisons)) != len(comparisons):
        raise DdlParseError("SORTED BY must not repeat column identifiers")
    return identifiers


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


def _literal_end(
    value: str,
    start: int,
    *,
    label: str,
    allow_signed_numeric: bool = False,
) -> int:
    """Return the end of one conservative Hive constant literal."""
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    if index >= len(value):
        raise DdlParseError(f"{label} must contain a constant")

    if value[index] in {"'", '"'}:
        return _quoted_literal_end(value, index)

    typed = re.match(
        r"(?:DATE|TIMESTAMP|TIMESTAMPLOCALTZ)\b",
        value[index:],
        flags=re.IGNORECASE,
    )
    if typed:
        return _quoted_literal_end(value, index + typed.end())

    keyword = re.match(r"(?:TRUE|FALSE|NULL)\b", value[index:], flags=re.IGNORECASE)
    if keyword:
        return index + keyword.end()

    numeric = _HIVE_NUMERIC_LITERAL.match(value, index)
    if numeric and (allow_signed_numeric or value[index] not in {"+", "-"}):
        return numeric.end()

    raise DdlParseError(f"{label} supports only constant literals")


def _top_level_as(value: str) -> tuple[str, str]:
    """Split CAST contents at its single top-level AS keyword."""
    quote: str | None = None
    round_depth = 0
    matches: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            index, quote = _quoted_scan_step(value, index, quote)
            continue
        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            continue
        if character == "(":
            round_depth += 1
        elif character == ")":
            round_depth -= 1
        elif round_depth == 0:
            match = re.match(r"AS\b", value[index:], flags=re.IGNORECASE)
            before = value[index - 1] if index else " "
            if match and not (before.isalnum() or before == "_"):
                matches.append((index, index + match.end()))
                index += match.end()
                continue
        index += 1
    if len(matches) != 1:
        raise DdlParseError("DEFAULT CAST must contain one top-level AS")
    start, end = matches[0]
    expression = value[:start].strip()
    data_type = value[end:].strip()
    if not expression or not data_type:
        raise DdlParseError("DEFAULT CAST must contain a value and primitive type")
    return expression, data_type


def _validate_default_cast_type(data_type: str) -> None:
    """Accept only built-in primitive Hive types as DEFAULT CAST targets."""
    if re.match(
        r"(?:ARRAY|MAP|STRUCT|UNIONTYPE)\b",
        data_type,
        flags=re.IGNORECASE,
    ):
        raise DdlParseError("DEFAULT CAST target must be a primitive Hive type")
    first_word = re.match(r"[A-Za-z_][A-Za-z0-9_]*", data_type)
    if first_word is None or first_word.group(0).upper() not in {
        *_SIMPLE_TYPES,
        "CHAR",
        "DECIMAL",
        "DOUBLE",
        "NUMERIC",
        "REAL",
        "VARCHAR",
    }:
        raise DdlParseError("DEFAULT CAST target must be a primitive Hive type")
    _validate_type(data_type)


def _default_value_end(value: str, start: int, *, depth: int = 0) -> int:
    """Parse the documented fail-closed subset of Hive DEFAULT values."""
    if depth > _MAX_DEFAULT_CAST_NESTING:
        raise DdlParseError(
            f"DEFAULT CAST nesting exceeds the supported limit of {_MAX_DEFAULT_CAST_NESTING}"
        )
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    if index >= len(value):
        raise DdlParseError("DEFAULT must contain a value")

    cast = re.match(r"CAST\b", value[index:], flags=re.IGNORECASE)
    if cast:
        contents, end = _parenthesized_contents(
            value,
            index + cast.end(),
            label="DEFAULT CAST",
        )
        expression, data_type = _top_level_as(contents)
        expression_end = _default_value_end(expression, 0, depth=depth + 1)
        if expression[expression_end:].strip():
            raise DdlParseError(
                "DEFAULT CAST supports only a literal, documented current-value function, "
                "NULL, or nested CAST"
            )
        _validate_default_cast_type(data_type)
        return end

    current_user = re.match(
        r"CURRENT_USER\s*\(\s*\)",
        value[index:],
        flags=re.IGNORECASE,
    )
    if current_user:
        return index + current_user.end()

    current_time = re.match(
        r"(?:CURRENT_TIME|CURRENT_DATE|CURRENT_TIMESTAMP)\b",
        value[index:],
        flags=re.IGNORECASE,
    )
    if current_time:
        end = index + current_time.end()
        empty_call = re.match(r"\s*\(\s*\)", value[end:])
        if empty_call:
            end += empty_call.end()
        return end

    try:
        return _literal_end(
            value,
            index,
            label="DEFAULT",
            allow_signed_numeric=True,
        )
    except DdlParseError:
        raise DdlParseError(
            "DEFAULT supports only literals, CURRENT_TIME/CURRENT_DATE/"
            "CURRENT_TIMESTAMP, CURRENT_USER(), NULL, or CAST of those values"
        ) from None


def _constraint_options_end(value: str, start: int, *, default: bool = False) -> int:
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    pattern = _DEFAULT_CONSTRAINT_OPTIONS if default else _GENERIC_CONSTRAINT_OPTIONS
    match = pattern.match(value, index)
    return match.end() if match else start


def _column_constraint_end(value: str) -> int:
    """Parse exactly one optional-name Hive column constraint."""
    index = 0
    prefix = re.match(rf"CONSTRAINT\s+{_IDENTIFIER}\s+", value, flags=re.IGNORECASE)
    if prefix:
        index = prefix.end()
    elif re.match(r"CONSTRAINT\b", value, flags=re.IGNORECASE):
        raise DdlParseError("malformed named column constraint")

    remaining = value[index:]
    match = re.match(r"NOT\s+NULL\b", remaining, flags=re.IGNORECASE)
    if match:
        end = index + match.end()
        return _constraint_options_end(value, end)

    match = re.match(r"DEFAULT\b", remaining, flags=re.IGNORECASE)
    if match:
        end = _default_value_end(value, index + match.end())
        return _constraint_options_end(value, end, default=True)

    match = re.match(r"CHECK\b", remaining, flags=re.IGNORECASE)
    if match:
        _balanced_expression_end(value, index + match.end())
        raise DdlParseError(
            "CHECK constraints are unsupported because expressions are not parsed safely"
        )

    match = re.match(r"(?:PRIMARY\s+KEY|UNIQUE)\b", remaining, flags=re.IGNORECASE)
    if match:
        end = index + match.end()
        return _constraint_options_end(value, end)

    match = re.match(
        rf"REFERENCES\s+{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?",
        remaining,
        flags=re.IGNORECASE,
    )
    if match:
        target_columns, end = _parenthesized_contents(
            value,
            index + match.end(),
            label="REFERENCES",
        )
        identifiers = _identifier_list(target_columns, label="REFERENCES")
        if len(identifiers) != 1:
            raise DdlParseError("a column REFERENCES constraint must name one target column")
        return _constraint_options_end(value, end)

    raise DdlParseError("unsupported or malformed column constraint")


def _parse_column_clauses(
    value: str,
    *,
    allow_constraints: bool = True,
) -> str | None:
    """Validate one optional constraint followed by one optional COMMENT."""
    remaining = value.strip()
    comment: str | None = None
    if not remaining:
        return None

    if not re.match(r"COMMENT\b", remaining, flags=re.IGNORECASE):
        if not allow_constraints:
            raise DdlParseError(
                "PARTITIONED BY columns may contain only a data type and optional COMMENT"
            )
        end = _column_constraint_end(remaining)
        remaining = remaining[end:].strip()

    if re.match(r"COMMENT\b", remaining, flags=re.IGNORECASE):
        comment, end = _quoted_literal(remaining, len("COMMENT"))
        remaining = remaining[end:].strip()

    if remaining:
        if not allow_constraints:
            raise DdlParseError(
                "PARTITIONED BY columns may contain only a data type and optional COMMENT"
            )
        raise DdlParseError(
            "a column may contain only one constraint followed by an optional COMMENT"
        )
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


def _validate_skewed_values(value: str, *, column_count: int) -> None:
    """Validate scalar skew values or constant tuples matching the skew-column arity."""
    parts = tuple(part.strip() for part in _top_level_parts(value))
    if not parts or any(not part for part in parts):
        raise DdlParseError("SKEWED BY ON must contain a non-empty constant list")

    tuple_flags = tuple(part.startswith("(") for part in parts)
    if any(tuple_flags) and not all(tuple_flags):
        raise DdlParseError("SKEWED BY ON cannot mix scalar constants and tuples")

    if not any(tuple_flags):
        if column_count != 1:
            raise DdlParseError(
                "SKEWED BY ON values for multiple columns must be tuples of matching arity"
            )
        for part in parts:
            end = _literal_end(part, 0, label="SKEWED BY ON")
            if part[end:].strip():
                raise DdlParseError("SKEWED BY ON values must be constants")
        return

    for part in parts:
        contents, end = _parenthesized_contents(part, 0, label="SKEWED BY ON tuple")
        if part[end:].strip():
            raise DdlParseError("SKEWED BY ON tuples must contain only constants")
        values = tuple(item.strip() for item in _top_level_parts(contents))
        if len(values) != column_count or any(not item for item in values):
            raise DdlParseError(
                "SKEWED BY ON tuple arity must match the number of SKEWED BY columns"
            )
        for item in values:
            item_end = _literal_end(item, 0, label="SKEWED BY ON")
            if item[item_end:].strip():
                raise DdlParseError("SKEWED BY ON tuple values must be constants")


def _parse_table_tail(
    value: str,
) -> tuple[tuple[Column, ...], str | None, tuple[_StructuralReference, ...]]:
    """Validate table clauses and retain structural column references."""
    remaining = value.strip()
    seen: set[str] = set()
    previous_order = -1
    partition_columns: tuple[Column, ...] = ()
    table_comment: str | None = None
    references: list[_StructuralReference] = []
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
            partition_columns, _ = _columns(
                contents,
                allow_constraints=False,
            )
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
            contents, end = _parenthesized_contents(
                remaining,
                start,
                label="CLUSTERED BY",
            )
            clustered_columns = _identifier_list(contents, label="CLUSTERED BY")
            references.append(("CLUSTERED BY", clustered_columns))
            remaining = remaining[end:].strip()
            if re.match(r"SORTED\b", remaining, flags=re.IGNORECASE):
                start = _consume_keyword(
                    remaining,
                    r"SORTED\s+BY",
                    label="SORTED BY",
                )
                contents, end = _parenthesized_contents(
                    remaining,
                    start,
                    label="SORTED BY",
                )
                sorted_columns = _sort_list(contents)
                references.append(("SORTED BY", sorted_columns))
                remaining = remaining[end:].strip()
            match = re.match(
                r"INTO\s+(?P<count>[0-9]+)\s+BUCKETS\b",
                remaining,
                flags=re.IGNORECASE,
            )
            if match is None:
                raise DdlParseError("malformed CLUSTERED BY clause")
            if not any(digit != "0" for digit in match.group("count")):
                raise DdlParseError("CLUSTERED BY bucket count must be positive")
            remaining = remaining[match.end() :].strip()
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
            contents, end = _parenthesized_contents(
                remaining,
                start,
                label="SKEWED BY",
            )
            skewed_columns = _identifier_list(contents, label="SKEWED BY")
            references.append(("SKEWED BY", skewed_columns))
            remaining = remaining[end:].strip()
            start = _consume_keyword(remaining, r"ON\b", label="SKEWED BY")
            skewed_values, end = _parenthesized_contents(
                remaining,
                start,
                label="SKEWED BY ON",
            )
            _validate_skewed_values(
                skewed_values,
                column_count=len(skewed_columns),
            )
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
            row_options = (
                ("FIELDS TERMINATED BY", r"FIELDS\s+TERMINATED\s+BY"),
                ("ESCAPED BY", r"ESCAPED\s+BY"),
                (
                    "COLLECTION ITEMS TERMINATED BY",
                    r"COLLECTION\s+ITEMS\s+TERMINATED\s+BY",
                ),
                ("MAP KEYS TERMINATED BY", r"MAP\s+KEYS\s+TERMINATED\s+BY"),
                ("LINES TERMINATED BY", r"LINES\s+TERMINATED\s+BY"),
                ("NULL DEFINED AS", r"NULL\s+DEFINED\s+AS"),
            )
            seen_row_options: set[str] = set()
            previous_row_option = -1
            while remaining and not re.match(
                (
                    r"(?:COMMENT|PARTITIONED|CLUSTERED|SKEWED|ROW|STORED|"
                    r"LOCATION|TBLPROPERTIES|AS)\b"
                ),
                remaining,
                flags=re.IGNORECASE,
            ):
                for option_order, (option, pattern) in enumerate(row_options):
                    match = re.match(pattern, remaining, flags=re.IGNORECASE)
                    if match:
                        if option in seen_row_options:
                            raise DdlParseError(
                                f"duplicate {option} option in ROW FORMAT DELIMITED"
                            )
                        if option_order < previous_row_option:
                            raise DdlParseError(
                                f"{option} option is out of order in ROW FORMAT DELIMITED"
                            )
                        if (
                            option == "ESCAPED BY"
                            and "FIELDS TERMINATED BY" not in seen_row_options
                        ):
                            raise DdlParseError(
                                "ESCAPED BY requires FIELDS TERMINATED BY in ROW FORMAT DELIMITED"
                            )
                        end = _quoted_literal_end(remaining, match.end())
                        seen_row_options.add(option)
                        previous_row_option = option_order
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
    return partition_columns, table_comment, tuple(references)


def _validate_table_constraint(definition: str) -> tuple[_StructuralReference, ...]:
    """Validate one table constraint and retain its local structural references."""
    index = 0
    prefix = re.match(
        rf"CONSTRAINT\s+{_IDENTIFIER}\s+",
        definition,
        flags=re.IGNORECASE,
    )
    if prefix:
        index = prefix.end()
    elif re.match(r"CONSTRAINT\b", definition, flags=re.IGNORECASE):
        raise DdlParseError("malformed named table constraint")

    remaining = definition[index:]
    match = re.match(r"(?P<kind>PRIMARY\s+KEY|UNIQUE)\b", remaining, flags=re.IGNORECASE)
    if match:
        columns, end = _parenthesized_contents(
            definition,
            index + match.end(),
            label=match.group("kind").upper(),
        )
        identifiers = _identifier_list(columns, label=match.group("kind").upper())
        options_end = _constraint_options_end(definition, end)
        if definition[options_end:].strip():
            raise DdlParseError("unsupported or malformed table constraint modifiers")
        label = "PRIMARY KEY" if match.group("kind").upper().startswith("PRIMARY") else "UNIQUE"
        return ((label, identifiers),)

    match = re.match(r"FOREIGN\s+KEY\b", remaining, flags=re.IGNORECASE)
    if match:
        local_columns, end = _parenthesized_contents(
            definition,
            index + match.end(),
            label="FOREIGN KEY",
        )
        local_identifiers = _identifier_list(local_columns, label="FOREIGN KEY")
        reference = re.match(
            rf"\s*REFERENCES\s+{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?",
            definition[end:],
            flags=re.IGNORECASE,
        )
        if reference is None:
            raise DdlParseError("malformed FOREIGN KEY REFERENCES clause")
        target_columns, target_end = _parenthesized_contents(
            definition,
            end + reference.end(),
            label="FOREIGN KEY REFERENCES",
        )
        target_identifiers = _identifier_list(
            target_columns,
            label="FOREIGN KEY REFERENCES",
        )
        if len(local_identifiers) != len(target_identifiers):
            raise DdlParseError("FOREIGN KEY and REFERENCES column lists must have matching arity")
        options_end = _constraint_options_end(definition, target_end)
        if definition[options_end:].strip():
            raise DdlParseError("unsupported or malformed table constraint modifiers")
        return (("FOREIGN KEY", local_identifiers),)

    match = re.match(r"CHECK\b", remaining, flags=re.IGNORECASE)
    if match:
        _balanced_expression_end(definition, index + match.end())
        raise DdlParseError(
            "CHECK constraints are unsupported because expressions are not parsed safely"
        )

    raise DdlParseError("unsupported or malformed table constraint")


def _statement_end(sql: str, start: int) -> int:
    quote: str | None = None
    index = start
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            index, quote = _quoted_scan_step(sql, index, quote)
            continue
        if character in {"'", '"', "`"}:
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
        if quote is not None:
            index, quote = _quoted_scan_step(raw_name, index, quote)
            continue
        if character == "`":
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
                self.index, remaining_quote = _quoted_scan_step(
                    self.value,
                    self.index,
                    quote,
                )
                if remaining_quote is None:
                    return
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
) -> tuple[tuple[Column, ...], tuple[_StructuralReference, ...]]:
    columns: list[Column] = []
    references: list[_StructuralReference] = []
    for raw_definition in _top_level_parts(column_list):
        definition = raw_definition.strip()
        if not definition:
            raise DdlParseError("empty column definition")
        first_word = definition.split(maxsplit=1)[0].upper()
        if first_word in _NON_COLUMN_PREFIXES:
            if not allow_constraints:
                raise DdlParseError("PARTITIONED BY must contain only column definitions")
            references.extend(_validate_table_constraint(definition))
            continue
        match = _COLUMN.match(definition)
        if match is None:
            raise DdlParseError("cannot parse a column definition")
        data_type, comment = _split_column_clauses(
            match.group("type"),
            allow_constraints=allow_constraints,
        )
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
    return tuple(columns), tuple(references)


def _validate_structural_references(
    columns: tuple[Column, ...],
    references: tuple[_StructuralReference, ...],
) -> None:
    """Resolve structural references against ordinary columns case-insensitively."""
    ordinary_columns = {_identifier_comparison(column.name) for column in columns}
    for label, names in references:
        for name in names:
            if _identifier_comparison(name) not in ordinary_columns:
                raise DdlParseError(f"{label} references unknown ordinary column {name!r}")


def parse_hive_ddl(sql: str) -> tuple[Table, ...]:
    """Parse all CREATE TABLE statements in a UTF-8 SQL document."""
    sql = sql.removeprefix("\ufeff")
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
        columns, constraint_references = _columns(cleaned[opening + 1 : closing])
        partition_columns, table_comment, tail_references = _parse_table_tail(
            cleaned[closing + 1 : tail_end]
        )
        _validate_structural_references(
            columns,
            constraint_references + tail_references,
        )
        database, name = _qualified_name(match.group("name"))
        tables.append(
            Table(
                database=database,
                name=name,
                columns=columns,
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
