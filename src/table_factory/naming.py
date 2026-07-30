"""Safe target-name derivation and semantic collision checks."""

from __future__ import annotations

import unicodedata

from table_factory.config import FactoryConfig
from table_factory.errors import SemanticValidationError
from table_factory.models import Table, TargetNames

_GREENPLUM_IDENTIFIER_BYTES = 63


def _comparison(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _is_safe_target_identifier(value: str) -> bool:
    return bool(value) and all(
        character.isalnum() or character in {"_", "-"} for character in value
    )


def _render_name_template(
    template: str,
    table: Table,
    *,
    label: str,
    replica: str,
    max_utf8_bytes: int | None = None,
) -> str:
    if "{source_database}" in template and table.database is None:
        raise SemanticValidationError(
            f"{label} uses source_database, but source table {table.name} is not database-qualified"
        )
    rendered = template.format(
        replica=replica,
        source_database=table.database or "",
        source_table=table.name,
    )
    if not _is_safe_target_identifier(rendered):
        raise SemanticValidationError(
            f"{label} produced invalid target identifier {rendered!r} "
            f"for source table {table.qualified_name}"
        )
    if max_utf8_bytes is not None and len(rendered.encode("utf-8")) > max_utf8_bytes:
        raise SemanticValidationError(
            f"{label} produced target identifier {rendered!r} longer than "
            f"{max_utf8_bytes} UTF-8 bytes"
        )
    return rendered


def _validate_column_names(table: Table) -> None:
    seen: dict[str, str] = {}
    for column in table.all_columns:
        comparison = _comparison(column.name)
        if comparison in seen:
            raise SemanticValidationError(
                f"source table {table.qualified_name} has colliding columns "
                f"{seen[comparison]!r} and {column.name!r}; ordinary and "
                "partition columns must be unique"
            )
        unsupported_query_characters = sorted(set(column.name) & {".", ":"})
        if unsupported_query_characters:
            rendered_characters = ", ".join(
                repr(character) for character in unsupported_query_characters
            )
            raise SemanticValidationError(
                f"source table {table.qualified_name} has Hive column "
                f"{column.name!r} containing {rendered_characters}; Hive cannot "
                "query column names containing dot or colon"
            )
        if len(column.name.encode("utf-8")) > _GREENPLUM_IDENTIFIER_BYTES:
            raise SemanticValidationError(
                f"source table {table.qualified_name} has column {column.name!r} "
                f"longer than Greenplum's {_GREENPLUM_IDENTIFIER_BYTES}-byte "
                "identifier limit"
            )
        seen[comparison] = column.name


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_source_values(table: Table) -> None:
    identifiers = [
        *(value for value in (table.database, table.name) if value is not None),
        *(column.name for column in table.all_columns),
    ]
    if any(_contains_control(identifier) for identifier in identifiers):
        raise SemanticValidationError(
            f"source table {table.qualified_name} contains a control character in an identifier"
        )
    comments = [
        *(value for value in (table.comment,) if value is not None),
        *(column.comment for column in table.all_columns if column.comment is not None),
    ]
    if any("\0" in comment for comment in comments):
        raise SemanticValidationError(
            f"source table {table.qualified_name} contains NUL in a comment"
        )


def build_target_names(table: Table, config: FactoryConfig) -> TargetNames:
    """Derive all target identifiers and reject destructive/colliding names."""
    _validate_source_values(table)
    _validate_column_names(table)
    hive_table = _render_name_template(
        config.hive.physical_table_name_template,
        table,
        label="hive.physical_table_name_template",
        replica=config.hive.replica,
    )
    external_table = _render_name_template(
        config.greenplum.external_table_name_template,
        table,
        label="greenplum.external_table_name_template",
        replica=config.greenplum.replica,
        max_utf8_bytes=_GREENPLUM_IDENTIFIER_BYTES,
    )
    physical_table = _render_name_template(
        config.greenplum.physical_table_name_template,
        table,
        label="greenplum.physical_table_name_template",
        replica=config.greenplum.replica,
        max_utf8_bytes=_GREENPLUM_IDENTIFIER_BYTES,
    )

    source_name_matches = _comparison(table.name) == _comparison(hive_table)
    source_database_matches = table.database is None or _comparison(table.database) == _comparison(
        config.hive.target_database
    )
    if source_name_matches and source_database_matches:
        raise SemanticValidationError(
            f"source Hive table {table.qualified_name} would collide with "
            f"target Hive table {config.hive.target_database}.{hive_table}"
        )

    same_greenplum_schema = _comparison(config.greenplum.external_schema) == _comparison(
        config.greenplum.physical_schema
    )
    same_greenplum_table = _comparison(external_table) == _comparison(physical_table)
    if same_greenplum_schema and same_greenplum_table:
        raise SemanticValidationError(
            "Greenplum external and physical targets would have the same "
            f"qualified name: {config.greenplum.external_schema}.{external_table}"
        )

    return TargetNames(
        hive_database=config.hive.target_database,
        hive_table=hive_table,
        greenplum_database=config.greenplum.database,
        greenplum_external_schema=config.greenplum.external_schema,
        greenplum_external_table=external_table,
        greenplum_physical_schema=config.greenplum.physical_schema,
        greenplum_physical_table=physical_table,
    )
