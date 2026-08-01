"""Rendering of the Liquibase wrapper for platform-managed external tables."""

from __future__ import annotations

import json

from table_factory.config import FactoryConfig
from table_factory.errors import SemanticValidationError
from table_factory.models import MappedColumn, TablePlan
from table_factory.sql import greenplum_qualified, greenplum_string

_DIRECT_TYPES = {
    "TEXT": "text",
    "SMALLINT": "int2",
    "INTEGER": "int4",
    "BIGINT": "int8",
    "REAL": "float4",
    "DOUBLE PRECISION": "float8",
    "BOOLEAN": "bool",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
}


def _factory_type(mapped: MappedColumn) -> str:
    greenplum_type = mapped.greenplum_type
    direct = _DIRECT_TYPES.get(greenplum_type)
    if direct is not None:
        return direct
    if greenplum_type.startswith(("NUMERIC(", "VARCHAR(", "CHAR(")):
        return "numeric" if greenplum_type.startswith("NUMERIC(") else "text"
    raise AssertionError(f"validated column has unsupported Greenplum type: {greenplum_type}")


def _column_description(mapped: MappedColumn) -> str:
    comment = mapped.column.comment
    if comment is None:
        return "-"
    if "\0" in comment:
        raise SemanticValidationError(f"source column {mapped.column.name!r} comment contains NUL")
    try:
        comment.encode("utf-8")
    except UnicodeEncodeError:
        raise SemanticValidationError(
            f"source column {mapped.column.name!r} comment contains invalid Unicode"
        ) from None
    return comment


def _changeset_id(plan: TablePlan, config: FactoryConfig) -> str:
    liquibase = config.greenplum.external.liquibase
    template = liquibase.changeset_id_template
    if "{source_database}" in template and plan.source.database is None:
        raise SemanticValidationError(
            "greenplum.external.liquibase.changeset_id_template uses source_database, "
            f"but source table {plan.source.name} is not database-qualified"
        )
    rendered = template.format(
        replica=config.greenplum.replica,
        source_database=plan.source.database or "",
        source_table=plan.source.name,
        external_table=plan.targets.greenplum_external_table,
    )
    if not rendered or any(
        not (character.isalnum() or character in {"_", ".", "-"}) for character in rendered
    ):
        raise SemanticValidationError(
            "greenplum.external.liquibase.changeset_id_template produced unsafe "
            f"changeset id {rendered!r} for source table {plan.source.qualified_name}"
        )
    return rendered


def _payload_json(plan: TablePlan, config: FactoryConfig) -> str:
    def encoded(value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = [
        "{",
        f'  "schema_name": {encoded(plan.targets.greenplum_external_schema)},',
        f'  "table_name": {encoded(plan.targets.greenplum_external_table)},',
        '  "source_table": '
        f"{encoded(f'{config.greenplum.original_hive_database}.{plan.source.name}')},",
        '  "columns": [',
    ]
    for index, mapped in enumerate(plan.mapped_columns):
        column = {
            "name": mapped.column.name,
            "type": _factory_type(mapped),
            "description": _column_description(mapped),
        }
        comma = "," if index + 1 < len(plan.mapped_columns) else ""
        lines.append(f"    {json.dumps(column, ensure_ascii=False)}{comma}")
    lines.extend(("  ]", "}"))
    return "\n".join(lines)


def render_greenplum_create_external_liquibase(
    plan: TablePlan,
    *,
    config: FactoryConfig,
) -> str:
    """Create a Liquibase changeset that delegates external-table creation."""
    schema = plan.targets.greenplum_external_schema
    table = plan.targets.greenplum_external_table
    liquibase = config.greenplum.external.liquibase
    target = greenplum_qualified(schema, table)
    function = greenplum_qualified(schema, liquibase.function_name)
    payload = greenplum_string(_payload_json(plan, config))
    return (
        "--liquibase formatted sql\n\n"
        f"--changeset {liquibase.author}:{_changeset_id(plan, config)} "
        "runOnChange:true splitStatements:false\n"
        f"DROP EXTERNAL TABLE IF EXISTS {target} CASCADE;\n\n"
        f"SELECT {function}({payload});\n"
    )
