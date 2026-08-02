from __future__ import annotations

from dataclasses import replace

import pytest

from table_factory.config import FactoryConfig
from table_factory.errors import DdlParseError, SemanticValidationError, TableFactoryError
from table_factory.generator import ensure_unique_artifacts, render_artifacts
from table_factory.parser import parse_hive_ddl
from table_factory.sql import hive_string


def test_one_leading_utf8_bom_is_ignored() -> None:
    table = parse_hive_ddl("\ufeffCREATE TABLE bom_table (id BIGINT);")[0]

    assert table.name == "bom_table"
    assert table.create_sql == "CREATE TABLE bom_table (id BIGINT);"


def test_only_one_leading_utf8_bom_is_ignored() -> None:
    with pytest.raises(DdlParseError, match="only CREATE TABLE statements are supported"):
        parse_hive_ddl("\ufeff\ufeffCREATE TABLE bom_table (id BIGINT);")


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


def test_backtick_column_identifier_may_end_with_a_backslash() -> None:
    table = parse_hive_ddl(r"CREATE TABLE escaped (`owner\` STRING);")[0]

    assert table.columns[0].name == "owner\\"


def test_backtick_partition_identifier_may_end_with_a_backslash() -> None:
    table = parse_hive_ddl(r"CREATE TABLE escaped (id INT) PARTITIONED BY (`owner\` STRING);")[0]

    assert table.partition_columns[0].name == "owner\\"


def test_doubled_backticks_are_preserved_before_a_terminal_backslash() -> None:
    table = parse_hive_ddl(
        r"CREATE TABLE escaped (`owner``\` STRING) "
        r"PARTITIONED BY (`partition``\` STRING);"
    )[0]

    assert table.columns[0].name == "owner`\\"
    assert table.partition_columns[0].name == "partition`\\"


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


def test_oversized_hive_type_parameter_is_a_domain_error() -> None:
    data_type = f"VARCHAR({'9' * 5_000})"

    with pytest.raises(DdlParseError, match="integer type parameter exceeds"):
        parse_hive_ddl(f"CREATE TABLE broken (value {data_type});")


def test_excessively_nested_hive_type_is_a_domain_error() -> None:
    data_type = f"{'ARRAY<' * 1_500}INT{'>' * 1_500}"

    with pytest.raises(DdlParseError, match="nesting exceeds the supported limit"):
        parse_hive_ddl(f"CREATE TABLE broken (value {data_type});")


def test_hive_type_nesting_at_supported_limit_is_accepted() -> None:
    data_type = f"{'ARRAY<' * 100}INT{'>' * 100}"

    table = parse_hive_ddl(f"CREATE TABLE nested (value {data_type});")[0]

    assert table.columns[0].data_type == data_type


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


@pytest.mark.parametrize(
    "unsafe_character",
    (
        "\u0085",  # Cc: NEXT LINE
        "\u202e",  # Cf: RIGHT-TO-LEFT OVERRIDE
        "\u2028",  # Zl: LINE SEPARATOR
        "\u2029",  # Zp: PARAGRAPH SEPARATOR
    ),
)
def test_unsafe_unicode_identifier_characters_are_rejected_without_echo(
    unsafe_character: str,
) -> None:
    table = parse_hive_ddl(f"CREATE TABLE source_db.events (`a{unsafe_character}b` STRING);")[0]

    with pytest.raises(
        SemanticValidationError,
        match="contains a control character",
    ) as raised:
        render_artifacts(
            table,
            config=FactoryConfig(),
            source_label="unsafe.sql",
        )

    assert unsafe_character not in str(raised.value)


def test_hive_comments_and_column_constraints_are_accepted() -> None:
    table = parse_hive_ddl(
        "CREATE TEMPORARY EXTERNAL TABLE constrained ("
        "payload STRUCT<name:STRING COMMENT 'display name', amount:DECIMAL(10,2)>,"
        'id BIGINT NOT NULL ENABLE COMMENT "required",'
        "status STRING DEFAULT 'active',"
        "price DOUBLE UNIQUE DISABLE NOVALIDATE RELY"
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


@pytest.mark.parametrize(
    "column_definition",
    (
        "id INT DEFAULT 1 DEFAULT 2",
        "id INT COMMENT 'identifier' NOT NULL",
        "id INT NOT NULL ENABLE DISABLE",
        "id INT NOT NULL NOT NULL",
        "id INT DEFAULT 1 RELY",
        "id INT DEFAULT 1 NOVALIDATE",
        "id INT DEFAULT 1 DISABLE ENABLE",
    ),
)
def test_column_constraint_order_and_cardinality_are_strict(
    column_definition: str,
) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(f"CREATE TABLE constrained ({column_definition});")


@pytest.mark.parametrize(
    "default_value",
    (
        "arbitrary_identifier",
        "now()",
        "(1 + 2)",
        "1 + 2",
        "array(1)",
    ),
)
def test_default_values_outside_the_documented_whitelist_are_rejected(
    default_value: str,
) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(f"CREATE TABLE constrained (value INT DEFAULT {default_value});")


def test_documented_default_values_and_modifiers_are_accepted() -> None:
    ddl = (
        "CREATE TABLE defaults ("
        "negative_value INT DEFAULT -1,"
        "label STRING CONSTRAINT label_default DEFAULT 'unknown' ENABLE,"
        "created DATE DEFAULT CURRENT_DATE(),"
        "updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP DISABLE,"
        "owner STRING DEFAULT CURRENT_USER(),"
        "nullable INT DEFAULT NULL,"
        "flag BOOLEAN DEFAULT TRUE,"
        "code VARCHAR(10) DEFAULT CAST('x' AS VARCHAR(10)) ENFORCED,"
        "nested_code STRING DEFAULT CAST(CAST('x' AS VARCHAR(10)) AS STRING)"
        ");"
    )

    table = parse_hive_ddl(ddl)[0]

    assert [column.name for column in table.columns] == [
        "negative_value",
        "label",
        "created",
        "updated",
        "owner",
        "nullable",
        "flag",
        "code",
        "nested_code",
    ]


def test_official_column_constraint_forms_and_comment_position_are_accepted() -> None:
    ddl = (
        "CREATE TABLE constrained ("
        "id BIGINT CONSTRAINT id_pk PRIMARY KEY DISABLE NOVALIDATE RELY "
        "COMMENT 'identifier',"
        "code STRING UNIQUE NOT ENFORCED NORELY,"
        "parent_id BIGINT REFERENCES parent(id) DISABLE NOVALIDATE,"
        "required STRING NOT NULL ENABLE NOVALIDATE RELY"
        ");"
    )

    table = parse_hive_ddl(ddl)[0]

    assert table.columns[0].comment == "identifier"


@pytest.mark.parametrize(
    "constraint",
    (
        "CHECK ()",
        "CHECK (id +)",
        "CHECK (id > 0)",
    ),
)
def test_column_check_constraints_fail_closed(constraint: str) -> None:
    with pytest.raises(DdlParseError, match="CHECK constraints are unsupported"):
        parse_hive_ddl(f"CREATE TABLE constrained (id INT {constraint});")


@pytest.mark.parametrize(
    "constraint",
    (
        "CHECK ()",
        "CHECK (id +)",
        "CONSTRAINT positive_id CHECK (id > 0)",
    ),
)
def test_table_check_constraints_fail_closed(constraint: str) -> None:
    with pytest.raises(DdlParseError, match="CHECK constraints are unsupported"):
        parse_hive_ddl(f"CREATE TABLE constrained (id INT, {constraint});")


def test_table_constraint_modifiers_follow_official_order_and_cardinality() -> None:
    valid = "CREATE TABLE constrained (id INT, PRIMARY KEY (id) DISABLE NOVALIDATE RELY);"

    assert parse_hive_ddl(valid)[0].name == "constrained"

    with pytest.raises(DdlParseError, match="constraint modifiers"):
        parse_hive_ddl("CREATE TABLE constrained (id INT, PRIMARY KEY (id) ENABLE DISABLE);")


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


def test_parenthesized_hive_clauses_support_backtick_identifiers() -> None:
    ddl = (
        "CREATE TABLE events (`owner's` STRING, `sort``'key` INT) "
        "PARTITIONED BY (`partition``er's` STRING COMMENT 'partition owner') "
        "CLUSTERED BY (`owner's`) "
        "SORTED BY (`sort``'key` DESC) INTO 2 BUCKETS;"
    )

    table = parse_hive_ddl(ddl)[0]

    assert [column.name for column in table.columns] == ["owner's", "sort`'key"]
    assert table.partition_columns[0].name == "partition`er's"
    assert table.partition_columns[0].comment == "partition owner"


@pytest.mark.parametrize(
    "tail",
    (
        "CLUSTERED BY () INTO 4 BUCKETS",
        "CLUSTERED BY (id +) INTO 4 BUCKETS",
        "CLUSTERED BY (id) SORTED BY (id +) INTO 4 BUCKETS",
        "CLUSTERED BY (id) SORTED BY (id DESC NULLS FIRST) INTO 4 BUCKETS",
        "SKEWED BY (id +) ON (1)",
    ),
)
def test_malformed_hive_identifier_and_sort_lists_are_rejected(tail: str) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(f"CREATE TABLE events (id BIGINT) {tail};")


@pytest.mark.parametrize(
    "tail",
    (
        "SKEWED BY (id) ON ()",
        "SKEWED BY (id) ON (id +)",
        "SKEWED BY (id) ON (-1)",
        "SKEWED BY (id) ON (1,,2)",
        "SKEWED BY (id) ON (SELECT * FROM source)",
        "SKEWED BY (id, category) ON (1, 2)",
        "SKEWED BY (id, category) ON ((1), (2))",
        "SKEWED BY (id, category) ON ((1, 'a'), (2))",
        "SKEWED BY (id) ON (1, (2))",
    ),
)
def test_skewed_by_on_rejects_malformed_values_and_wrong_tuple_arity(tail: str) -> None:
    with pytest.raises(DdlParseError, match="SKEWED BY ON"):
        parse_hive_ddl(f"CREATE TABLE events (id BIGINT, category STRING) {tail};")


@pytest.mark.parametrize(
    "tail",
    (
        "SKEWED BY (id) ON (1, 2, NULL, 'unknown')",
        "SKEWED BY (id) ON ((1), (2))",
        ("SKEWED BY (id, category) ON ((1, 'one'), (2, 'two')) STORED AS DIRECTORIES"),
    ),
)
def test_skewed_by_on_accepts_constants_and_matching_tuples(tail: str) -> None:
    ddl = f"CREATE TABLE events (id BIGINT, category STRING) {tail};"

    assert parse_hive_ddl(ddl)[0].create_sql == ddl


@pytest.mark.parametrize(
    ("columns", "tail"),
    (
        ("id BIGINT", "CLUSTERED BY (missing) INTO 4 BUCKETS"),
        (
            "id BIGINT",
            "CLUSTERED BY (id) SORTED BY (missing DESC) INTO 4 BUCKETS",
        ),
        ("id BIGINT", "SKEWED BY (missing) ON (1)"),
        (
            "id BIGINT, PRIMARY KEY (missing) DISABLE NOVALIDATE",
            "",
        ),
        (
            "id BIGINT, UNIQUE (missing) DISABLE NOVALIDATE",
            "",
        ),
        (
            "id BIGINT, FOREIGN KEY (missing) REFERENCES parent(id) DISABLE NOVALIDATE",
            "",
        ),
    ),
)
def test_structural_references_must_name_ordinary_columns(
    columns: str,
    tail: str,
) -> None:
    with pytest.raises(DdlParseError, match="unknown ordinary column 'missing'"):
        parse_hive_ddl(f"CREATE TABLE events ({columns}) {tail};")


def test_structural_references_are_resolved_case_insensitively() -> None:
    ddl = (
        "CREATE TABLE events ("
        "`MiXeD` BIGINT, Category STRING,"
        "PRIMARY KEY (mixed) DISABLE NOVALIDATE,"
        "UNIQUE (CATEGORY) DISABLE NOVALIDATE,"
        "FOREIGN KEY (MIXED) REFERENCES parent(id) DISABLE NOVALIDATE"
        ") "
        "CLUSTERED BY (MIXED) SORTED BY (category DESC) INTO 4 BUCKETS "
        "SKEWED BY (mixed, CATEGORY) ON ((1, 'one'));"
    )

    assert parse_hive_ddl(ddl)[0].name == "events"


def test_structural_reference_matching_uses_nfkc_casefolding() -> None:
    ddl = "CREATE TABLE events (`Ｋey` BIGINT) CLUSTERED BY (`key`) INTO 2 BUCKETS;"

    assert parse_hive_ddl(ddl)[0].name == "events"


@pytest.mark.parametrize("bucket_count", ("0", "000"))
def test_clustered_by_requires_a_positive_bucket_count(bucket_count: str) -> None:
    with pytest.raises(DdlParseError, match="bucket count must be positive"):
        parse_hive_ddl(
            f"CREATE TABLE events (id BIGINT) CLUSTERED BY (id) INTO {bucket_count} BUCKETS;"
        )


def test_clustered_by_requires_an_ascii_bucket_count() -> None:
    with pytest.raises(DdlParseError, match="malformed CLUSTERED BY clause"):
        parse_hive_ddl("CREATE TABLE events (id BIGINT) CLUSTERED BY (id) INTO ١ BUCKETS;")


@pytest.mark.parametrize(
    "options",
    (
        "FIELDS TERMINATED BY ',' FIELDS TERMINATED BY '|'",
        "COLLECTION ITEMS TERMINATED BY ':' FIELDS TERMINATED BY ','",
        r"ESCAPED BY '\\'",
        r"FIELDS TERMINATED BY ',' ESCAPED BY '\\' ESCAPED BY '!'",
    ),
)
def test_row_format_delimited_option_order_and_uniqueness_are_validated(
    options: str,
) -> None:
    with pytest.raises(DdlParseError):
        parse_hive_ddl(f"CREATE TABLE events (id BIGINT) ROW FORMAT DELIMITED {options};")


def test_row_format_delimited_options_in_official_order_are_accepted() -> None:
    ddl = (
        r"CREATE TABLE events (id BIGINT) ROW FORMAT DELIMITED "
        r"FIELDS TERMINATED BY ',' ESCAPED BY '\\' "
        r"COLLECTION ITEMS TERMINATED BY ':' MAP KEYS TERMINATED BY '=' "
        r"LINES TERMINATED BY '\n' NULL DEFINED AS '\N';"
    )

    assert parse_hive_ddl(ddl)[0].create_sql == ddl


@pytest.mark.parametrize(
    "partition_definition",
    (
        "p INT NOT NULL",
        "p INT DEFAULT 0",
        "p INT CHECK (p > 0)",
        "p INT PRIMARY KEY",
        "p INT COMMENT 'partition key' NOT NULL",
    ),
)
def test_partition_columns_reject_non_comment_constraints(
    partition_definition: str,
) -> None:
    with pytest.raises(DdlParseError, match="data type and optional COMMENT"):
        parse_hive_ddl(f"CREATE TABLE events (id BIGINT) PARTITIONED BY ({partition_definition});")


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
