"""Rendering of Greenplum external/physical tables and their INSERT."""

from __future__ import annotations

from table_factory.config import FactoryConfig
from table_factory.models import TablePlan
from table_factory.sql import (
    greenplum_identifier,
    greenplum_qualified,
    greenplum_string,
)


def _mapped_column_definitions(plan: TablePlan) -> str:
    return ",\n".join(
        f"  {greenplum_identifier(mapped.column.name)} {mapped.greenplum_type}"
        for mapped in plan.mapped_columns
    )


def _comments(plan: TablePlan, *, schema: str, table: str) -> str:
    qualified = greenplum_qualified(schema, table)
    statements: list[str] = []
    if plan.source.comment is not None:
        statements.append(
            f"COMMENT ON TABLE {qualified} IS {greenplum_string(plan.source.comment)};"
        )
    for mapped in plan.mapped_columns:
        if mapped.column.comment is None:
            continue
        statements.append(
            f"COMMENT ON COLUMN {qualified}."
            f"{greenplum_identifier(mapped.column.name)} IS "
            f"{greenplum_string(mapped.column.comment)};"
        )
    return "\n".join(statements)


def _external_location(plan: TablePlan, config: FactoryConfig) -> str:
    external = config.greenplum.external
    return external.location_template.format(
        subscription=config.greenplum.subscription,
        original_hive_database=config.greenplum.original_hive_database,
        hive_table=plan.targets.hive_table,
        profile=external.profile,
        server=external.server,
    )


def render_greenplum_create_external(
    plan: TablePlan,
    *,
    config: FactoryConfig,
) -> str:
    """Create a PXF-backed external table over the new physical Hive table."""
    schema = plan.targets.greenplum_external_schema
    table = plan.targets.greenplum_external_table
    qualified = greenplum_qualified(schema, table)
    external = config.greenplum.external
    rendered = (
        f"CREATE EXTERNAL TABLE {qualified} (\n"
        f"{_mapped_column_definitions(plan)}\n"
        ")\n"
        "LOCATION (\n"
        f"  {greenplum_string(_external_location(plan, config))}\n"
        ") ON ALL\n"
        f"FORMAT {greenplum_string(external.format.kind.upper())} "
        f"(FORMATTER={greenplum_string(external.format.formatter)})\n"
        "ENCODING 'UTF8';\n"
    )
    comments = _comments(plan, schema=schema, table=table)
    return rendered if not comments else f"{rendered}\n{comments}\n"


def render_greenplum_create_physical(
    plan: TablePlan,
    *,
    config: FactoryConfig,
) -> str:
    """Create a regular Greenplum table with deterministic distribution."""
    schema = plan.targets.greenplum_physical_schema
    table = plan.targets.greenplum_physical_table
    qualified = greenplum_qualified(schema, table)
    if config.greenplum.distribution.mode != "random":
        raise AssertionError("validated configuration has an unknown distribution")
    rendered = (
        f"CREATE TABLE {qualified} (\n"
        f"{_mapped_column_definitions(plan)}\n"
        ")\n"
        "DISTRIBUTED RANDOMLY;\n"
    )
    comments = _comments(plan, schema=schema, table=table)
    return rendered if not comments else f"{rendered}\n{comments}\n"


def render_greenplum_insert(plan: TablePlan) -> str:
    """Insert external rows into the physical table without SELECT star."""
    target = greenplum_qualified(
        plan.targets.greenplum_physical_schema,
        plan.targets.greenplum_physical_table,
    )
    source = greenplum_qualified(
        plan.targets.greenplum_external_schema,
        plan.targets.greenplum_external_table,
    )
    target_columns = ",\n".join(
        f"  {greenplum_identifier(mapped.column.name)}" for mapped in plan.mapped_columns
    )
    source_columns = ",\n".join(
        f"  {greenplum_identifier(mapped.column.name)}" for mapped in plan.mapped_columns
    )
    return (
        f"INSERT INTO {target} (\n{target_columns}\n)\nSELECT\n{source_columns}\nFROM {source};\n"
    )
