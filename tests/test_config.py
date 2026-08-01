from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

import table_factory.config as config_module
from table_factory.config import ARTIFACT_ROLES, FactoryConfig, load_config
from table_factory.errors import ConfigurationError
from table_factory.generator import render_artifacts
from table_factory.parser import parse_hive_ddl

VALID_CONFIG: dict[str, Any] = {
    "version": 3,
    "output": {
        "include_source_comment": True,
        "filename_separator": "__",
        "artifacts": {role: True for role in ARTIFACT_ROLES},
    },
    "hive": {
        "target_database": "target_hive_db",
        "replica": "hive_r",
        "physical_table_name_template": "{replica}_{source_table}_physical",
        "insert_mode": "into",
        "storage": {
            "format": "textfile",
            "field_delimiter": ",",
            "escape_character": "\\",
            "null_value": "\\N",
        },
    },
    "greenplum": {
        "database": "target_gp_database",
        "external_schema": "ext",
        "physical_schema": "dwh",
        "replica": "gp_r",
        "subscription": "sub_a",
        "original_hive_database": "original_hive_db",
        "external_table_name_template": "{replica}_{source_table}_ext",
        "physical_table_name_template": "{replica}_{source_table}",
        "distribution": {
            "mode": "random",
        },
        "external": {
            "location_template": (
                "pxf://prx_{subscription}_{original_hive_database}.{hive_table}"
                "?PROFILE={profile}&SERVER={server}"
            ),
            "profile": "hive",
            "server": "default",
            "format": {
                "kind": "custom",
                "formatter": "pxfwritable_import",
            },
            "liquibase": {
                "author": "22643610",
                "changeset_id_template": "stg-{source_table}_ext",
                "function_name": "f_create_external_table",
            },
        },
    },
}


def _write_config(tmp_path: Path, raw: object) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return config


def _changed(path: str, value: object) -> dict[str, Any]:
    raw = deepcopy(VALID_CONFIG)
    parts = path.split(".")
    parent: dict[str, Any] = raw
    for part in parts[:-1]:
        parent = parent[part]
    parent[parts[-1]] = value
    return raw


def _missing(path: str) -> dict[str, Any]:
    raw = deepcopy(VALID_CONFIG)
    parts = path.split(".")
    parent: dict[str, Any] = raw
    for part in parts[:-1]:
        parent = parent[part]
    del parent[parts[-1]]
    return raw


def _with_unknown_key(path: str, key: str = "unexpected") -> dict[str, Any]:
    raw = deepcopy(VALID_CONFIG)
    parent: dict[str, Any] = raw
    if path:
        for part in path.split("."):
            parent = parent[part]
    parent[key] = "value"
    return raw


def _load(tmp_path: Path, raw: object) -> Any:
    path = _write_config(tmp_path, raw)
    return load_config(path, display_name=path.name)


def test_version_3_configuration_loads_all_supported_settings(
    tmp_path: Path,
) -> None:
    config = _load(tmp_path, VALID_CONFIG)

    assert config.version == 3
    assert config.output.include_source_comment is True
    assert config.output.filename_separator == "__"
    assert config.output.enabled_artifacts == frozenset(ARTIFACT_ROLES)
    assert config.hive.target_database == "target_hive_db"
    assert config.hive.replica == "hive_r"
    assert config.hive.physical_table_name_template == "{replica}_{source_table}_physical"
    assert config.hive.insert_mode == "into"
    assert config.hive.storage.format == "textfile"
    assert config.hive.storage.field_delimiter == ","
    assert config.hive.storage.escape_character == "\\"
    assert config.hive.storage.null_value == "\\N"
    assert config.greenplum.database == "target_gp_database"
    assert config.greenplum.external_schema == "ext"
    assert config.greenplum.physical_schema == "dwh"
    assert config.greenplum.replica == "gp_r"
    assert config.greenplum.subscription == "sub_a"
    assert config.greenplum.original_hive_database == "original_hive_db"
    assert config.greenplum.external_table_name_template == "{replica}_{source_table}_ext"
    assert config.greenplum.physical_table_name_template == "{replica}_{source_table}"
    assert config.greenplum.distribution.mode == "random"
    assert config.greenplum.external.location_template == (
        "pxf://prx_{subscription}_{original_hive_database}.{hive_table}"
        "?PROFILE={profile}&SERVER={server}"
    )
    assert config.greenplum.external.profile == "hive"
    assert config.greenplum.external.server == "default"
    assert config.greenplum.external.format.kind == "custom"
    assert config.greenplum.external.format.formatter == "pxfwritable_import"
    assert config.greenplum.external.liquibase.author == "22643610"
    assert config.greenplum.external.liquibase.changeset_id_template == "stg-{source_table}_ext"
    assert config.greenplum.external.liquibase.function_name == "f_create_external_table"
    assert config.as_dict() == VALID_CONFIG


def test_programmatic_defaults_follow_the_new_location_contract() -> None:
    config = FactoryConfig()
    table = parse_hive_ddl("CREATE TABLE source_db.events (id BIGINT);")[0]

    artifacts = render_artifacts(
        table,
        config=config,
        source_label="events.sql",
    )

    assert config.greenplum.subscription == "subscription"
    assert config.greenplum.original_hive_database == "original_hive_database"
    assert config.output.enabled_artifacts == frozenset(ARTIFACT_ROLES)
    assert (
        "pxf://prx_subscription_original_hive_database.replica_events_physical"
        "?PROFILE=hive&SERVER=default"
    ) in artifacts[2].content


@pytest.mark.parametrize(
    "artifacts_override",
    [None, {}],
    ids=["section-absent", "empty-override"],
)
def test_artifact_selection_defaults_every_role_to_enabled(
    tmp_path: Path,
    artifacts_override: dict[str, bool] | None,
) -> None:
    raw = deepcopy(VALID_CONFIG)
    if artifacts_override is None:
        del raw["output"]["artifacts"]
    else:
        raw["output"]["artifacts"] = artifacts_override

    config = _load(tmp_path, raw)

    assert config.output.enabled_artifacts == frozenset(ARTIFACT_ROLES)
    assert config.as_dict()["output"]["artifacts"] == {role: True for role in ARTIFACT_ROLES}


@pytest.mark.parametrize("enabled_role", ARTIFACT_ROLES)
def test_artifact_selection_can_emit_each_role_independently(
    tmp_path: Path,
    enabled_role: str,
) -> None:
    raw = deepcopy(VALID_CONFIG)
    raw["output"]["artifacts"] = {role: role == enabled_role for role in ARTIFACT_ROLES}
    config = _load(tmp_path, raw)
    table = parse_hive_ddl("CREATE TABLE source_db.events (id BIGINT);")[0]

    artifacts = render_artifacts(table, config=config, source_label="events.sql")

    assert [artifact.filename for artifact in artifacts] == [
        f"source_db_events__{enabled_role}.sql"
    ]


def test_disabled_liquibase_artifact_is_not_rendered_or_validated(
    tmp_path: Path,
) -> None:
    raw = deepcopy(VALID_CONFIG)
    raw["output"]["artifacts"] = {
        "01_hive_create_physical": True,
        "03_greenplum_create_external_liquibase": False,
    }
    raw["greenplum"]["external"]["liquibase"]["changeset_id_template"] = (
        "{source_database}-{source_table}"
    )
    config = _load(tmp_path, raw)
    table = parse_hive_ddl("CREATE TABLE events (id BIGINT);")[0]

    artifacts = render_artifacts(table, config=config, source_label="events.sql")

    assert all("liquibase" not in artifact.filename for artifact in artifacts)


def test_all_disabled_artifacts_do_not_invoke_renderers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = deepcopy(VALID_CONFIG)
    raw["output"]["artifacts"] = {role: False for role in ARTIFACT_ROLES}
    config = _load(tmp_path, raw)
    table = parse_hive_ddl("CREATE TABLE source_db.events (id BIGINT);")[0]

    def unexpected_renderer(*args: object, **kwargs: object) -> str:
        raise AssertionError("disabled renderer was called")

    renderer_names = (
        "render_hive_create_physical",
        "render_hive_insert",
        "render_greenplum_create_external",
        "render_greenplum_create_external_liquibase",
        "render_greenplum_create_physical",
        "render_greenplum_insert",
    )
    for renderer_name in renderer_names:
        monkeypatch.setattr(
            f"table_factory.generator.{renderer_name}",
            unexpected_renderer,
        )

    assert render_artifacts(table, config=config, source_label="events.sql") == ()


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
def test_versions_1_and_2_have_explicit_migration_errors(
    tmp_path: Path,
    version: int,
) -> None:
    old_config = {
        "version": version,
        "dialect": "hive",
        "output": {
            "include_source_comment": True,
            "filename_separator": "__",
        },
    }

    with pytest.raises(ConfigurationError) as captured:
        _load(tmp_path, old_config)

    assert str(captured.value) == (
        f"configuration version {version} is no longer supported; migrate to version 3"
    )


@pytest.mark.parametrize(
    ("contents", "line", "column"),
    [
        (
            "version: 2\nversion: 3\n",
            2,
            1,
        ),
        (
            "version: 3\nhive:\n  target_database: first\n  target_database: second\n",
            4,
            3,
        ),
    ],
    ids=["top-level-version", "nested-mapping"],
)
def test_configuration_rejects_duplicate_mapping_keys_at_every_level(
    tmp_path: Path,
    contents: str,
    line: int,
    column: int,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigurationError) as captured:
        load_config(config, display_name=config.name)

    assert str(captured.value) == (
        "cannot read configuration config.yaml: "
        f"found duplicate mapping key at line {line}, column {column}"
    )
    assert str(tmp_path) not in str(captured.value)


def test_configuration_treats_an_implicit_invalid_timestamp_as_a_string(
    tmp_path: Path,
) -> None:
    config = _write_config(tmp_path, VALID_CONFIG)
    contents = config.read_text(encoding="utf-8")
    assert "target_database: target_hive_db" in contents
    config.write_text(
        contents.replace(
            "target_database: target_hive_db",
            "target_database: 2023-02-30",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_config(config, display_name=config.name)

    assert str(captured.value) == (
        "hive.target_database must be one unqualified SQL identifier using "
        "letters, digits and underscores"
    )


def test_configuration_rejects_an_oversized_yaml_integer_before_conversion(
    tmp_path: Path,
) -> None:
    config = _write_config(tmp_path, VALID_CONFIG)
    contents = config.read_text(encoding="utf-8")
    config.write_text(
        contents.replace("version: 3", f"version: {'9' * 5000}", 1),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_config(config, display_name=config.name)

    assert "integer value exceeds 128 characters" in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_configuration_rejects_excessive_yaml_nesting_before_python_recursion(
    tmp_path: Path,
) -> None:
    config = _write_config(tmp_path, VALID_CONFIG)
    contents = config.read_text(encoding="utf-8")
    nested_value = "[" * 1500 + "0" + "]" * 1500
    config.write_text(f"{contents}\nunexpected: {nested_value}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as captured:
        load_config(config, display_name=config.name)

    assert "configuration nesting exceeds 64 levels" in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_configuration_normalizes_explicit_timestamp_constructor_errors(
    tmp_path: Path,
) -> None:
    config = _write_config(tmp_path, VALID_CONFIG)
    contents = config.read_text(encoding="utf-8")
    config.write_text(
        f"{contents}\nunexpected: !!timestamp 2023-02-30\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_config(config, display_name=config.name)

    assert str(captured.value) == ("cannot read configuration config.yaml: invalid YAML value")


def test_configuration_rejects_files_larger_than_the_bounded_read_limit(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_bytes(b" " * (1024 * 1024 + 1))

    with pytest.raises(ConfigurationError) as captured:
        load_config(config, display_name=config.name)

    assert str(captured.value) == (
        "cannot read configuration config.yaml: file exceeds 1048576 bytes"
    )


@pytest.mark.parametrize(
    "value",
    [True, False, "3", 3.0, None, [], {}, 0, 4],
    ids=[
        "true",
        "false",
        "string",
        "float",
        "null",
        "sequence",
        "mapping",
        "zero",
        "future",
    ],
)
def test_configuration_requires_version_3_as_an_integer(
    tmp_path: Path,
    value: object,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        _load(tmp_path, _changed("version", value))

    assert str(captured.value) == "configuration version must be 3"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "configuration must be a YAML mapping"),
        (
            _changed("output", []),
            "output must be a YAML mapping",
        ),
        (
            _changed("output.include_source_comment", 1),
            "output.include_source_comment must be a boolean",
        ),
        (
            _changed("output.artifacts", []),
            "output.artifacts must be a YAML mapping",
        ),
        (
            _changed("output.artifacts.01_hive_create_physical", 1),
            "output.artifacts.01_hive_create_physical must be a boolean",
        ),
        (
            _changed("hive.target_database", 7),
            "hive.target_database must be a string",
        ),
        (
            _changed("hive.replica", None),
            "hive.replica must be a string",
        ),
        (
            _changed("hive.storage", "textfile"),
            "hive.storage must be a YAML mapping",
        ),
        (
            _changed("greenplum.replica", 7),
            "greenplum.replica must be a string",
        ),
        (
            _changed("greenplum.subscription", 7),
            "greenplum.subscription must be a string",
        ),
        (
            _changed("greenplum.original_hive_database", None),
            "greenplum.original_hive_database must be a string",
        ),
        (
            _changed("greenplum.distribution.mode", False),
            "greenplum.distribution.mode must be a string",
        ),
        (
            _changed("greenplum.external.format", "custom"),
            "greenplum.external.format must be a YAML mapping",
        ),
        (
            _changed("greenplum.external.liquibase", "changeset"),
            "greenplum.external.liquibase must be a YAML mapping",
        ),
        (
            _changed("greenplum.external.liquibase.author", 22643610),
            "greenplum.external.liquibase.author must be a string",
        ),
    ],
)
def test_configuration_rejects_wrong_value_types(
    tmp_path: Path,
    raw: object,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _load(tmp_path, raw)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("version", "configuration is missing required key: version"),
        ("greenplum", "configuration is missing required key: greenplum"),
        (
            "output.filename_separator",
            "output is missing required key: filename_separator",
        ),
        (
            "hive.target_database",
            "hive is missing required key: target_database",
        ),
        (
            "hive.replica",
            "hive is missing required key: replica",
        ),
        (
            "hive.storage.null_value",
            "hive.storage is missing required key: null_value",
        ),
        (
            "greenplum.external_schema",
            "greenplum is missing required key: external_schema",
        ),
        (
            "greenplum.replica",
            "greenplum is missing required key: replica",
        ),
        (
            "greenplum.subscription",
            "greenplum is missing required key: subscription",
        ),
        (
            "greenplum.original_hive_database",
            "greenplum is missing required key: original_hive_database",
        ),
        (
            "greenplum.distribution.mode",
            "greenplum.distribution is missing required key: mode",
        ),
        (
            "greenplum.external.profile",
            "greenplum.external is missing required key: profile",
        ),
        (
            "greenplum.external.format.formatter",
            "greenplum.external.format is missing required key: formatter",
        ),
        (
            "greenplum.external.liquibase.author",
            "greenplum.external.liquibase is missing required key: author",
        ),
        (
            "greenplum.external.liquibase.changeset_id_template",
            "greenplum.external.liquibase is missing required key: changeset_id_template",
        ),
        (
            "greenplum.external.liquibase.function_name",
            "greenplum.external.liquibase is missing required key: function_name",
        ),
    ],
)
def test_configuration_rejects_missing_required_keys(
    tmp_path: Path,
    path: str,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _load(tmp_path, _missing(path))


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("", "configuration"),
        ("output", "output"),
        ("output.artifacts", "output.artifacts"),
        ("hive", "hive"),
        ("hive.storage", "hive.storage"),
        ("greenplum", "greenplum"),
        ("greenplum.distribution", "greenplum.distribution"),
        ("greenplum.external", "greenplum.external"),
        ("greenplum.external.format", "greenplum.external.format"),
        ("greenplum.external.liquibase", "greenplum.external.liquibase"),
    ],
)
def test_configuration_rejects_unknown_keys_at_every_level(
    tmp_path: Path,
    path: str,
    label: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"{label} contains unknown key: unexpected",
    ):
        _load(tmp_path, _with_unknown_key(path))


def test_configuration_escapes_terminal_controls_in_unknown_keys(
    tmp_path: Path,
) -> None:
    unsafe_key = "evil\x1b[2J"

    with pytest.raises(ConfigurationError) as captured:
        _load(tmp_path, _with_unknown_key("", key=unsafe_key))

    message = str(captured.value)
    assert message == r"configuration contains unknown key: evil\u001b[2J"
    assert "\x1b" not in message


@pytest.mark.parametrize(
    "path",
    [
        "hive.target_database",
        "greenplum.original_hive_database",
        "greenplum.database",
        "greenplum.external_schema",
        "greenplum.physical_schema",
        "greenplum.external.liquibase.function_name",
    ],
)
def test_configuration_rejects_empty_namespaces(
    tmp_path: Path,
    path: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"{path} must not be empty",
    ):
        _load(tmp_path, _changed(path, ""))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("hive.target_database", "target.analytics"),
        ("greenplum.original_hive_database", "original.database"),
        ("greenplum.database", "warehouse-prod"),
        ("greenplum.external_schema", "ext;DROP_SCHEMA"),
        ("greenplum.physical_schema", "dwh schema"),
        ("greenplum.external.liquibase.function_name", "admin.create_external"),
    ],
)
def test_configuration_rejects_invalid_qualified_names(
    tmp_path: Path,
    path: str,
    value: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"{path} must be one unqualified SQL identifier",
    ):
        _load(tmp_path, _changed(path, value))


@pytest.mark.parametrize(
    "path",
    [
        "greenplum.database",
        "greenplum.external_schema",
        "greenplum.physical_schema",
        "greenplum.external.liquibase.function_name",
    ],
)
def test_greenplum_namespaces_reject_more_than_63_utf8_bytes(
    tmp_path: Path,
    path: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"{path} must contain at most 63 UTF-8 bytes",
    ):
        _load(tmp_path, _changed(path, "a" * 64))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("hive.replica", "0"),
        ("hive.replica", "_"),
        ("hive.replica", "hive-replica_01"),
        ("greenplum.replica", "9"),
        ("greenplum.replica", "_gp"),
        ("greenplum.replica", "gp-replica_01"),
        ("greenplum.subscription", "7"),
        ("greenplum.subscription", "_sub"),
        ("greenplum.subscription", "sub-name_01"),
    ],
)
def test_ascii_identifier_fragments_accept_supported_values(
    tmp_path: Path,
    path: str,
    value: str,
) -> None:
    config = _load(tmp_path, _changed(path, value))

    if path == "hive.replica":
        assert config.hive.replica == value
    elif path == "greenplum.replica":
        assert config.greenplum.replica == value
    else:
        assert config.greenplum.subscription == value


@pytest.mark.parametrize(
    "path",
    ["hive.replica", "greenplum.replica", "greenplum.subscription"],
)
def test_ascii_identifier_fragments_reject_empty_values(
    tmp_path: Path,
    path: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"{path} must not be empty",
    ):
        _load(tmp_path, _changed(path, ""))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("hive.replica", "-hive"),
        ("hive.replica", "hive.replica"),
        ("hive.replica", "hive replica"),
        ("hive.replica", "реплика"),
        ("greenplum.replica", "-gp"),
        ("greenplum.replica", "gp/replica"),
        ("greenplum.replica", "réplica"),
        ("greenplum.subscription", "-sub"),
        ("greenplum.subscription", "sub.name"),
        ("greenplum.subscription", "подписка"),
    ],
)
def test_ascii_identifier_fragments_reject_unsafe_values(
    tmp_path: Path,
    path: str,
    value: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=(
            rf"{path} must be an ASCII identifier fragment starting with a letter, "
            r"digit or underscore and containing only letters, digits, underscores and hyphens"
        ),
    ):
        _load(tmp_path, _changed(path, value))


def test_naming_and_location_placeholders_resolve_from_their_own_fields(
    tmp_path: Path,
) -> None:
    config = _load(tmp_path, VALID_CONFIG)
    table = parse_hive_ddl("CREATE TABLE source_db.events (id BIGINT);")[0]

    artifacts = render_artifacts(
        table,
        config=config,
        source_label="events.sql",
    )

    assert "CREATE TABLE `target_hive_db`.`hive_r_events_physical`" in artifacts[0].content
    assert 'CREATE EXTERNAL TABLE "ext"."gp_r_events_ext"' in artifacts[2].content
    assert (
        "pxf://prx_sub_a_original_hive_db.hive_r_events_physical?PROFILE=hive&SERVER=default"
    ) in artifacts[2].content
    assert 'CREATE TABLE "dwh"."gp_r_events"' in artifacts[4].content


@pytest.mark.parametrize(
    "path",
    [
        "hive.physical_table_name_template",
        "greenplum.external_table_name_template",
        "greenplum.physical_table_name_template",
    ],
)
def test_name_templates_reject_unknown_placeholders(
    tmp_path: Path,
    path: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"{path} contains unknown placeholder: tenant",
    ):
        _load(tmp_path, _changed(path, "{tenant}_{source_table}"))


def test_changeset_id_template_rejects_unknown_placeholders(tmp_path: Path) -> None:
    path = "greenplum.external.liquibase.changeset_id_template"

    with pytest.raises(
        ConfigurationError,
        match=rf"{path} contains unknown placeholder: subscription",
    ):
        _load(tmp_path, _changed(path, "stg-{subscription}-{source_table}"))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            "pxf://prx_{subscription}_{original_hive_database}.{hive_table}"
            "?PROFILE={profile}&SERVER={server}&USER={user}",
            "contains unknown placeholder: user",
        ),
        (
            "pxf://prx_{subscription}_{original_hive_database}.{hive_table}?PROFILE={profile}",
            "is missing required placeholder: server",
        ),
        (
            "pxf://prx_{original_hive_database}.{hive_table}?PROFILE={profile}&SERVER={server}",
            "is missing required placeholder: subscription",
        ),
        (
            "pxf://prx_{subscription}_{hive_table}?PROFILE={profile}&SERVER={server}",
            "is missing required placeholder: original_hive_database",
        ),
        (
            "https://prx_{subscription}_{original_hive_database}.{hive_table}"
            "?PROFILE={profile}&SERVER={server}",
            "must start with 'pxf://'",
        ),
        (
            "pxf://prx_{replica}_{hive_database}.{hive_table}?PROFILE={profile}&SERVER={server}",
            "contains unknown placeholder: replica",
        ),
        (
            "pxf://prx_{subscription}_{hive_database}.{hive_table}"
            "?PROFILE={profile}&SERVER={server}",
            "contains unknown placeholder: hive_database",
        ),
    ],
)
def test_location_template_enforces_its_placeholder_contract(
    tmp_path: Path,
    value: str,
    message: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"greenplum.external.location_template {message}",
    ):
        _load(
            tmp_path,
            _changed("greenplum.external.location_template", value),
        )


@pytest.mark.parametrize(
    "value",
    [
        "pxf://prx_{subscription}_{hive_table}.{original_hive_database}"
        "?PROFILE={profile}&SERVER={server}",
        "pxf://{subscription}_{original_hive_database}.{hive_table}"
        "?PROFILE={profile}&SERVER={server}",
        "pxf://prx_{subscription}_{original_hive_database}.{hive_table}"
        "?PROFILE={profile}&SERVER={server}&PASSWORD=secret",
        "pxf://prx_{subscription}_{original_hive_database}/{hive_table}"
        "?PROFILE={profile}&SERVER={server}",
        "pxf://prx_{subscription}_{original_hive_database}.{hive_table}"
        "?PROFILE={server}&SERVER={profile}",
        "pxf://prx_{subscription}_{original_hive_database}.{hive_table}"
        "?profile={profile}&SERVER={server}",
    ],
)
def test_location_template_rejects_semantic_variants(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=(
            r"greenplum.external.location_template must use "
            r"'pxf://prx_\{subscription\}_\{original_hive_database\}\.\{hive_table\}' "
            r"and only "
            r"PROFILE=\{profile\} and SERVER=\{server\} query parameters"
        ),
    ):
        _load(
            tmp_path,
            _changed("greenplum.external.location_template", value),
        )


def test_location_template_rejects_escaped_required_braces(
    tmp_path: Path,
) -> None:
    value = (
        "pxf://prx_{subscription}_{{original_hive_database}}.{hive_table}"
        "?PROFILE={profile}&SERVER={server}"
    )

    with pytest.raises(
        ConfigurationError,
        match=(
            r"greenplum.external.location_template "
            r"is missing required placeholder: original_hive_database"
        ),
    ):
        _load(
            tmp_path,
            _changed("greenplum.external.location_template", value),
        )


def test_location_template_accepts_profile_and_server_in_reversed_order(
    tmp_path: Path,
) -> None:
    value = (
        "pxf://prx_{subscription}_{original_hive_database}.{hive_table}"
        "?SERVER={server}&PROFILE={profile}"
    )

    config = _load(
        tmp_path,
        _changed("greenplum.external.location_template", value),
    )

    assert config.greenplum.external.location_template == value
    table = parse_hive_ddl("CREATE TABLE source_db.events (id BIGINT);")[0]
    artifacts = render_artifacts(
        table,
        config=config,
        source_label="events.sql",
    )
    assert (
        "pxf://prx_sub_a_original_hive_db.hive_r_events_physical?SERVER=default&PROFILE=hive"
    ) in artifacts[2].content


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            "hive.physical_table_name_template",
            "{source_table};DROP_TABLE",
            "contains unsafe literal characters",
        ),
        (
            "greenplum.external_table_name_template",
            "{source_table!r}",
            "placeholders cannot use conversion or formatting",
        ),
        (
            "greenplum.physical_table_name_template",
            "{source_table:>20}",
            "placeholders cannot use conversion or formatting",
        ),
        (
            "greenplum.external.location_template",
            "pxf://prx_{subscription}_{original_hive_database}.{hive_table}'"
            "?PROFILE={profile}&SERVER={server}",
            "contains unsafe URI characters",
        ),
        (
            "greenplum.external.liquibase.changeset_id_template",
            "stg {source_table}",
            "contains unsafe literal characters",
        ),
    ],
)
def test_configuration_rejects_unsafe_templates(
    tmp_path: Path,
    path: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"{path} {message}",
    ):
        _load(tmp_path, _changed(path, value))


def test_configuration_rejects_unsupported_hive_storage(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=(
            r"hive.storage.format must be 'textfile'; "
            r"other formats are not supported end-to-end"
        ),
    ):
        _load(tmp_path, _changed("hive.storage.format", "parquet"))


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        (
            "HdfsTextSimple",
            "must be 'hive' for the supported textfile/Hive workflow",
        ),
        (
            "Hive;DROP",
            "contains unsafe characters",
        ),
    ],
)
def test_configuration_rejects_unsupported_or_unsafe_external_profiles(
    tmp_path: Path,
    profile: str,
    message: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"greenplum.external.profile {message}",
    ):
        _load(tmp_path, _changed("greenplum.external.profile", profile))


def test_supported_external_profile_is_rendered_in_canonical_lowercase(
    tmp_path: Path,
) -> None:
    config = _load(
        tmp_path,
        _changed("greenplum.external.profile", "hIVE"),
    )

    assert config.greenplum.external.profile == "hive"


@pytest.mark.parametrize(
    ("author", "message"),
    [
        ("", "must not be empty"),
        ("22643610:override", "contains unsafe characters"),
        ("author value", "contains unsafe characters"),
        ("bad\noption", "must not contain control characters"),
    ],
)
def test_liquibase_author_rejects_empty_or_unsafe_values(
    tmp_path: Path,
    author: str,
    message: str,
) -> None:
    path = "greenplum.external.liquibase.author"

    with pytest.raises(ConfigurationError, match=rf"{path} {message}"):
        _load(tmp_path, _changed(path, author))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("greenplum.external.format.kind", "csv"),
        ("greenplum.external.format.formatter", "text"),
    ],
)
def test_configuration_rejects_incompatible_external_formats(
    tmp_path: Path,
    path: str,
    value: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=(
            r"greenplum.external.format must use kind 'custom' with formatter "
            r"'pxfwritable_import' for textfile/Hive compatibility"
        ),
    ):
        _load(tmp_path, _changed(path, value))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("hive.storage.field_delimiter", "||"),
        ("hive.storage.escape_character", "\\\\"),
    ],
)
def test_storage_delimiter_and_escape_must_be_one_character(
    tmp_path: Path,
    path: str,
    value: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"{path} must contain exactly one character",
    ):
        _load(tmp_path, _changed(path, value))


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            "hive.storage.field_delimiter",
            "é",
            "must be one printable ASCII character",
        ),
        (
            "hive.storage.escape_character",
            "é",
            "must be one printable ASCII character",
        ),
        (
            "hive.storage.field_delimiter",
            "\u200b",
            "must be one printable ASCII character",
        ),
        (
            "hive.storage.escape_character",
            "\u200b",
            "must be one printable ASCII character",
        ),
        (
            "hive.storage.field_delimiter",
            "1",
            "must not be a digit because Hive interprets numeric delimiters as byte codes",
        ),
        (
            "hive.storage.escape_character",
            "1",
            "must not be a digit because Hive interprets numeric delimiters as byte codes",
        ),
    ],
)
def test_storage_delimiter_and_escape_reject_unsafe_characters(
    tmp_path: Path,
    path: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"{path} {message}",
    ):
        _load(tmp_path, _changed(path, value))


def test_storage_delimiter_and_escape_must_differ(tmp_path: Path) -> None:
    raw = _changed("hive.storage.field_delimiter", "\\")

    with pytest.raises(
        ConfigurationError,
        match=(r"hive.storage.field_delimiter and escape_character must differ"),
    ):
        _load(tmp_path, raw)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            "NULL,VALUE",
            "must not contain hive.storage.field_delimiter",
        ),
        (
            "не null",
            "must contain printable ASCII only",
        ),
        (
            "\u200b",
            "must contain printable ASCII only",
        ),
        (
            "NULL",
            "must contain hive.storage.escape_character",
        ),
        (
            "NULL\\",
            "must not end with hive.storage.escape_character",
        ),
    ],
)
def test_storage_null_value_rejects_ambiguous_or_unsafe_values(
    tmp_path: Path,
    value: str,
    message: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=rf"hive.storage.null_value {message}",
    ):
        _load(tmp_path, _changed("hive.storage.null_value", value))


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            "output.filename_separator",
            "",
            "output.filename_separator must not be empty",
        ),
        (
            "output.filename_separator",
            "++",
            "output.filename_separator may contain",
        ),
        (
            "hive.insert_mode",
            "append",
            "hive.insert_mode must be 'into' or 'overwrite'",
        ),
        (
            "hive.storage.null_value",
            "",
            "hive.storage.null_value must not be empty",
        ),
    ],
)
def test_configuration_rejects_invalid_output_and_insert_settings(
    tmp_path: Path,
    path: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _load(tmp_path, _changed(path, value))


def test_configuration_rejects_unsupported_distribution_mode(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match=r"greenplum.distribution.mode must be 'random'",
    ):
        _load(tmp_path, _changed("greenplum.distribution.mode", "hash"))


def test_configuration_io_error_does_not_expose_an_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path, VALID_CONFIG)

    def fail_to_open(_path: Path, flags: int) -> int:
        raise PermissionError(13, "Permission denied", str(config))

    monkeypatch.setattr(config_module.os, "open", fail_to_open)

    with pytest.raises(ConfigurationError) as captured:
        load_config(config, display_name=str(config))

    assert str(config) not in str(captured.value)
    assert config.name in str(captured.value)
    assert "Permission denied" in str(captured.value)
