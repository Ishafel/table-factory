"""Rendering of the physical Hive table and its source-to-target INSERT."""

from __future__ import annotations

from table_factory.config import FactoryConfig
from table_factory.models import TablePlan
from table_factory.sql import hive_identifier, hive_qualified, hive_string


def _column_definition(name: str, data_type: str, comment: str | None) -> str:
    rendered = f"  {hive_identifier(name)} {data_type}"
    if comment is not None:
        rendered += f" COMMENT {hive_string(comment)}"
    return rendered


def render_hive_create_physical(
    plan: TablePlan,
    *,
    config: FactoryConfig,
) -> str:
    """Create a non-external, non-partitioned target owned by Hive."""
    target = hive_qualified(
        plan.targets.hive_database,
        plan.targets.hive_table,
    )
    columns = ",\n".join(
        _column_definition(column.name, column.data_type, column.comment)
        for column in plan.source.all_columns
    )
    table_comment = (
        f"\nCOMMENT {hive_string(plan.source.comment)}" if plan.source.comment is not None else ""
    )
    storage = config.hive.storage
    return (
        f"CREATE TABLE {target} (\n"
        f"{columns}\n"
        f"){table_comment}\n"
        "ROW FORMAT DELIMITED\n"
        f"  FIELDS TERMINATED BY {hive_string(storage.field_delimiter)}\n"
        f"  ESCAPED BY {hive_string(storage.escape_character)}\n"
        f"  NULL DEFINED AS {hive_string(storage.null_value)}\n"
        f"STORED AS {storage.format.upper()};\n"
    )


def render_hive_insert(plan: TablePlan, *, config: FactoryConfig) -> str:
    """Insert source rows with an explicit target and source column order."""
    target = hive_qualified(
        plan.targets.hive_database,
        plan.targets.hive_table,
    )
    source = hive_qualified(plan.source.database, plan.source.name)
    target_columns = ",\n".join(
        f"  {hive_identifier(column.name)}" for column in plan.source.all_columns
    )
    source_columns = ",\n".join(
        f"  {hive_identifier(column.name)}" for column in plan.source.all_columns
    )
    if config.hive.insert_mode == "overwrite":
        return f"INSERT OVERWRITE TABLE {target}\nSELECT\n{source_columns}\nFROM {source};\n"
    return (
        f"INSERT INTO TABLE {target} (\n"
        f"{target_columns}\n"
        ")\n"
        "SELECT\n"
        f"{source_columns}\n"
        f"FROM {source};\n"
    )
