from __future__ import annotations

from dataclasses import replace

import pytest

from table_factory.config import FactoryConfig
from table_factory.errors import DdlParseError, TableFactoryError
from table_factory.generator import ensure_unique_artifacts, render_artifacts
from table_factory.parser import parse_hive_ddl
from table_factory.sql import hive_string


def test_create_table_text_inside_comment_literal_is_not_a_second_table() -> None:
    tables = parse_hive_ddl(
        "CREATE TABLE real (note STRING COMMENT 'CREATE TABLE phantom (id INT)');"
    )

    assert [table.name for table in tables] == ["real"]


def test_create_table_text_inside_backtick_identifier_is_not_a_second_table() -> None:
    ddl = "CREATE TABLE real (`CREATE TABLE phantom (id INT)` STRING);"

    assert [table.name for table in parse_hive_ddl(ddl)] == ["real"]


@pytest.mark.parametrize(
    "ddl",
    (
        'CREATE TABLE "quoted_table" (id INT);',
        'CREATE TABLE plain ("quoted_column" INT);',
        'CREATE TABLE plain (payload STRUCT<"quoted_field":STRING>);',
        "CREATE TABLE plain (payload STRUCT<dashed-field:STRING>);",
        "CREATE TABLE plain (payload STRUCT<dollar$field:STRING>);",
        "CREATE TABLE dashed-name (id INT);",
        "CREATE TABLE dollar$name (id INT);",
        "CREATE TABLE plain (dashed-name INT);",
        "CREATE TABLE plain (dollar$name INT);",
    ),
)
def test_non_hive_identifier_quoting_and_characters_are_rejected(ddl: str) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(ddl)


def test_qualified_table_name_inside_one_backtick_pair_is_supported() -> None:
    table = parse_hive_ddl("CREATE TABLE `sales.daily` (`order.id` BIGINT);")[0]

    assert table.database == "sales"
    assert table.name == "daily"
    assert table.columns[0].name == "order.id"


def test_single_backtick_qualified_name_renders_as_two_source_identifiers() -> None:
    table = parse_hive_ddl("CREATE TABLE `sales.daily` (id BIGINT);")[0]

    artifacts = render_artifacts(
        table,
        config=FactoryConfig(),
        source_label="daily.sql",
    )

    assert "FROM `sales`.`daily`;" in artifacts[1].content


def test_quoted_qualified_table_name_unescapes_each_resulting_part() -> None:
    table = parse_hive_ddl("CREATE TABLE `sales``archive.daily``events` (`order``id` BIGINT);")[0]

    assert table.database == "sales`archive"
    assert table.name == "daily`events"
    assert table.columns[0].name == "order`id"


@pytest.mark.parametrize(
    "name",
    (
        "`database.schema.table`",
        "`database..table`",
        "`database.`",
        "`.table`",
    ),
)
def test_malformed_qualified_table_name_inside_backticks_is_rejected(name: str) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(f"CREATE TABLE {name} (id BIGINT);")


@pytest.mark.parametrize(
    "name",
    (
        "database.table",
        "`database`.`table`",
        "database.`table`",
        "`database`.table",
    ),
)
def test_existing_qualified_table_name_forms_remain_supported(name: str) -> None:
    table = parse_hive_ddl(f"CREATE TABLE {name} (id BIGINT);")[0]

    assert table.database == "database"
    assert table.name == "table"


def test_doubled_identifier_quotes_are_unescaped() -> None:
    table = parse_hive_ddl("CREATE TABLE `sales``daily` (`order``id` BIGINT);")[0]

    assert table.name == "sales`daily"
    assert table.columns[0].name == "order`id"


def test_hive_comment_escapes_are_decoded_and_rendered_losslessly() -> None:
    ddl = (
        r"CREATE TABLE escaped (value STRING COMMENT "
        r"'\Z|\%|\_|\u0416|\101|\f|\uD83D\uDE00|\u000c');"
    )

    comment = parse_hive_ddl(ddl)[0].columns[0].comment

    assert comment == "\x1a|\\%|\\_|Ж|A|f|😀|\f"
    assert hive_string(comment) == r"'\Z|\%|\_|Ж|A|f|😀|\u000c'"


def test_unpaired_unicode_surrogate_in_comment_is_rejected() -> None:
    ddl = r"CREATE TABLE escaped (value STRING COMMENT '\uD83D');"

    with pytest.raises(DdlParseError, match="invalid Unicode surrogate escape"):
        parse_hive_ddl(ddl)


@pytest.mark.parametrize(
    "data_type",
    (
        "ARRAY<STRING",
        "CHAR(256)",
        "DECIMAL(10,)",
        "DECIMAL(39, 1)",
        "INT name STRING",
        "MAP<ARRAY<INT>, STRING>",
        "VARCHAR(65536)",
    ),
)
def test_malformed_hive_types_are_rejected(data_type: str) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(f"CREATE TABLE broken (value {data_type});")


@pytest.mark.parametrize(
    "columns",
    (
        ", id INT",
        "id INT,, name STRING",
        "id INT,",
        "id INT COMMENT nonsense",
        "id INT COMMENT 'valid' WAT",
    ),
)
def test_malformed_column_lists_are_rejected(columns: str) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(f"CREATE TABLE broken ({columns});")


def test_nested_hive_types_are_accepted() -> None:
    table = parse_hive_ddl(
        "CREATE TABLE nested_types ("
        "attributes MAP<STRING, ARRAY<DECIMAL(12, 2)>>,"
        "payload STRUCT<name:STRING, flags:ARRAY<BOOLEAN>>"
        ");"
    )[0]

    assert [column.name for column in table.columns] == ["attributes", "payload"]


def test_hive_comments_and_column_constraints_are_accepted() -> None:
    table = parse_hive_ddl(
        "CREATE TEMPORARY EXTERNAL TABLE constrained ("
        "payload STRUCT<name:STRING COMMENT 'display name', amount:DECIMAL(10,2)>,"
        'id BIGINT NOT NULL ENABLE COMMENT "required",'
        "status STRING DEFAULT 'active',"
        "price DOUBLE CHECK (price > 0) RELY"
        ");"
    )[0]

    assert table.name == "constrained"
    assert [column.name for column in table.columns] == [
        "payload",
        "id",
        "status",
        "price",
    ]
    assert table.external is True
    assert table.columns[1].comment == "required"


def test_table_clauses_are_validated_and_preserved_verbatim() -> None:
    ddl = (
        "CREATE EXTERNAL TABLE events (id BIGINT COMMENT 'identifier') "
        'COMMENT "events table" '
        "PARTITIONED BY (event_day DATE) "
        "ROW FORMAT DELIMITED FIELDS TERMINATED BY ',' "
        "STORED AS PARQUET "
        "LOCATION '/warehouse/events' "
        "TBLPROPERTIES ('owner'='analytics');"
    )

    table = parse_hive_ddl(ddl)[0]

    assert table.create_sql == ddl
    assert table.external is True
    assert table.comment == "events table"
    assert [column.name for column in table.partition_columns] == ["event_day"]


@pytest.mark.parametrize(
    "tail",
    (
        "PARTITIONED BY (event_day DATE) COMMENT 'late comment'",
        "CLUSTERED BY (id) INTO 4 BUCKETS PARTITIONED BY (event_day DATE)",
        "SKEWED BY (id) ON (1) CLUSTERED BY (id) INTO 4 BUCKETS",
        "ROW FORMAT DELIMITED SKEWED BY (id) ON (1)",
        "STORED AS PARQUET ROW FORMAT DELIMITED",
        "LOCATION '/warehouse/events' STORED AS PARQUET",
        "TBLPROPERTIES ('owner'='analytics') LOCATION '/warehouse/events'",
    ),
)
def test_table_clauses_out_of_official_order_are_rejected(tail: str) -> None:
    with pytest.raises(DdlParseError, match="out of order"):
        parse_hive_ddl(f"CREATE TABLE events (id BIGINT) {tail};")


@pytest.mark.parametrize(
    "tail",
    (
        "COMMENT 'one' COMMENT 'two'",
        "PARTITIONED BY (day_one DATE) PARTITIONED BY (day_two DATE)",
        ("CLUSTERED BY (id) INTO 4 BUCKETS CLUSTERED BY (id) INTO 8 BUCKETS"),
        "SKEWED BY (id) ON (1) SKEWED BY (id) ON (2)",
        "ROW FORMAT DELIMITED ROW FORMAT DELIMITED",
        "STORED AS PARQUET STORED AS ORC",
        "LOCATION '/one' LOCATION '/two'",
        "TBLPROPERTIES ('one'='1') TBLPROPERTIES ('two'='2')",
    ),
)
def test_duplicate_table_clauses_are_rejected(tail: str) -> None:
    with pytest.raises(DdlParseError, match="duplicate"):
        parse_hive_ddl(f"CREATE TABLE events (id BIGINT) {tail};")


@pytest.mark.parametrize(
    "ddl",
    (
        "CREATE TABLE t(id INT) AS SELECT",
        "CREATE TABLE t (id INT) AS SELECT id FROM source;",
        "CREATE TABLE t AS SELECT id FROM source;",
        "CREATE TABLE t LIKE source;",
    ),
)
def test_ctas_and_like_forms_are_rejected_as_unsupported(ddl: str) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(ddl)


@pytest.mark.parametrize(
    "ddl",
    (
        "CREATE TABLE t (id INT) THIS IS INVALID;",
        "CREATE TABLE t (id INT); DROP TABLE victim;",
        "CREATE TABLE t (id INT, CONSTRAINT definitely_not_hive);",
        "CREATE TABLE t (id INT) PARTITIONED BY (definitely not hive);",
        "CREATE TABLE t (id INT) TBLPROPERTIES (THIS IS INVALID);",
    ),
)
def test_non_hive_tail_and_statements_are_rejected(ddl: str) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(ddl)


def test_stored_by_with_serde_properties_is_accepted() -> None:
    ddl = (
        "CREATE TABLE handled (id INT) "
        "STORED BY 'example.StorageHandler' "
        "WITH SERDEPROPERTIES ('separator'=',');"
    )

    assert parse_hive_ddl(ddl)[0].create_sql == ddl


def test_artifact_names_are_portable_across_case_insensitive_filesystems() -> None:
    upper, lower = parse_hive_ddl("CREATE TABLE Foo (id INT); CREATE TABLE foo (id INT);")
    artifacts = [
        *render_artifacts(upper, config=FactoryConfig(), source_label="input.sql"),
        *render_artifacts(lower, config=FactoryConfig(), source_label="input.sql"),
    ]

    with pytest.raises(TableFactoryError, match="same output name"):
        ensure_unique_artifacts(artifacts)


def test_long_unicode_table_name_stays_within_filesystem_component_limit() -> None:
    table_name = "表" * 120
    table = parse_hive_ddl(f"CREATE TABLE `{table_name}` (id INT);")[0]
    default = FactoryConfig()
    config = FactoryConfig(
        hive=replace(
            default.hive,
            physical_table_name_template="safe_hive_target",
        ),
        greenplum=replace(
            default.greenplum,
            external_table_name_template="safe_external_target",
            physical_table_name_template="safe_physical_target",
        ),
    )

    artifacts = render_artifacts(
        table,
        config=config,
        source_label="line\nbreak.sql",
    )

    assert all(len(artifact.filename.encode("utf-8")) <= 255 for artifact in artifacts)
    assert all("line\nbreak" not in artifact.content for artifact in artifacts)
