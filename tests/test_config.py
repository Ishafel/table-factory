from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from table_factory.config import load_config
from table_factory.errors import ConfigurationError

VALID_CONFIG: dict[str, Any] = {
    "version": 2,
    "output": {
        "include_source_comment": True,
        "filename_separator": "__",
    },
    "hive": {
        "target_database": "target_hive_db",
        "physical_table_name_template": "{source_database}_{source_table}_physical",
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
        "external_table_name_template": "{source_table}_ext",
        "physical_table_name_template": "{source_table}",
        "distribution": {
            "mode": "random",
        },
        "external": {
            "location_template": (
                "pxf://{hive_database}.{hive_table}?PROFILE={profile}&SERVER={server}"
            ),
            "profile": "Hive",
            "server": "default",
            "format": {
                "kind": "custom",
                "formatter": "pxfwritable_import",
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


def test_version_2_configuration_loads_all_supported_settings(
    tmp_path: Path,
) -> None:
    config = _load(tmp_path, VALID_CONFIG)

    assert config.version == 2
    assert config.output.include_source_comment is True
    assert config.output.filename_separator == "__"
    assert config.hive.target_database == "target_hive_db"
    assert config.hive.physical_table_name_template == "{source_database}_{source_table}_physical"
    assert config.hive.insert_mode == "into"
    assert config.hive.storage.format == "textfile"
    assert config.hive.storage.field_delimiter == ","
    assert config.hive.storage.escape_character == "\\"
    assert config.hive.storage.null_value == "\\N"
    assert config.greenplum.database == "target_gp_database"
    assert config.greenplum.external_schema == "ext"
    assert config.greenplum.physical_schema == "dwh"
    assert config.greenplum.external_table_name_template == "{source_table}_ext"
    assert config.greenplum.physical_table_name_template == "{source_table}"
    assert config.greenplum.distribution.mode == "random"
    assert config.greenplum.external.location_template == (
        "pxf://{hive_database}.{hive_table}?PROFILE={profile}&SERVER={server}"
    )
    assert config.greenplum.external.profile == "Hive"
    assert config.greenplum.external.server == "default"
    assert config.greenplum.external.format.kind == "custom"
    assert config.greenplum.external.format.formatter == "pxfwritable_import"
    assert config.as_dict() == VALID_CONFIG


def test_version_1_configuration_has_an_explicit_migration_error(
    tmp_path: Path,
) -> None:
    old_config = {
        "version": 1,
        "dialect": "hive",
        "output": {
            "include_source_comment": True,
            "filename_separator": "__",
        },
    }

    with pytest.raises(
        ConfigurationError,
        match=r"version 1 .*no longer supported.*migrate to version 2",
    ):
        _load(tmp_path, old_config)


@pytest.mark.parametrize(
    ("contents", "line", "column"),
    [
        (
            "version: 1\nversion: 2\n",
            2,
            1,
        ),
        (
            "version: 2\nhive:\n  target_database: first\n  target_database: second\n",
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


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "configuration must be a YAML mapping"),
        (
            _changed("version", True),
            "configuration version must be 2",
        ),
        (
            _changed("version", "2"),
            "configuration version must be 2",
        ),
        (
            _changed("output", []),
            "output must be a YAML mapping",
        ),
        (
            _changed("output.include_source_comment", 1),
            "output.include_source_comment must be a boolean",
        ),
        (
            _changed("hive.target_database", 7),
            "hive.target_database must be a string",
        ),
        (
            _changed("hive.storage", "textfile"),
            "hive.storage must be a YAML mapping",
        ),
        (
            _changed("greenplum.distribution.mode", False),
            "greenplum.distribution.mode must be a string",
        ),
        (
            _changed("greenplum.external.format", "custom"),
            "greenplum.external.format must be a YAML mapping",
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
            "hive.storage.null_value",
            "hive.storage is missing required key: null_value",
        ),
        (
            "greenplum.external_schema",
            "greenplum is missing required key: external_schema",
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
        ("hive", "hive"),
        ("hive.storage", "hive.storage"),
        ("greenplum", "greenplum"),
        ("greenplum.distribution", "greenplum.distribution"),
        ("greenplum.external", "greenplum.external"),
        ("greenplum.external.format", "greenplum.external.format"),
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


@pytest.mark.parametrize(
    "path",
    [
        "hive.target_database",
        "greenplum.database",
        "greenplum.external_schema",
        "greenplum.physical_schema",
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
        ("greenplum.database", "warehouse-prod"),
        ("greenplum.external_schema", "ext;DROP_SCHEMA"),
        ("greenplum.physical_schema", "dwh schema"),
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


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            "pxf://{hive_database}.{hive_table}?PROFILE={profile}&SERVER={server}&USER={user}",
            "contains unknown placeholder: user",
        ),
        (
            "pxf://{hive_database}.{hive_table}?PROFILE={profile}",
            "is missing required placeholder: server",
        ),
        (
            "https://{hive_database}.{hive_table}?PROFILE={profile}&SERVER={server}",
            "must start with 'pxf://'",
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
        "pxf://{hive_table}.{hive_database}?PROFILE={profile}&SERVER={server}",
        ("pxf://{hive_database}.{hive_table}?PROFILE={profile}&SERVER={server}&PASSWORD=secret"),
        "pxf://{hive_database}/{hive_table}?PROFILE={profile}&SERVER={server}",
        "pxf://{hive_database}.{hive_table}?PROFILE={server}&SERVER={profile}",
        "pxf://{hive_database}.{hive_table}?profile={profile}&SERVER={server}",
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
            r"'pxf://\{hive_database\}\.\{hive_table\}' and only "
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
    value = "pxf://{{hive_database}}.{hive_table}?PROFILE={profile}&SERVER={server}"

    with pytest.raises(
        ConfigurationError,
        match=(
            r"greenplum.external.location_template "
            r"is missing required placeholder: hive_database"
        ),
    ):
        _load(
            tmp_path,
            _changed("greenplum.external.location_template", value),
        )


def test_location_template_accepts_profile_and_server_in_reversed_order(
    tmp_path: Path,
) -> None:
    value = "pxf://{hive_database}.{hive_table}?SERVER={server}&PROFILE={profile}"

    config = _load(
        tmp_path,
        _changed("greenplum.external.location_template", value),
    )

    assert config.greenplum.external.location_template == value


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
            "pxf://{hive_database}.{hive_table}'?PROFILE={profile}&SERVER={server}",
            "contains unsafe URI characters",
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
            "must be 'Hive' for the supported textfile/Hive workflow",
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


def test_supported_external_profile_is_rendered_with_canonical_case(
    tmp_path: Path,
) -> None:
    config = _load(
        tmp_path,
        _changed("greenplum.external.profile", "hIVE"),
    )

    assert config.greenplum.external.profile == "Hive"


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

    def fail_to_read(_path: Path, *, encoding: str) -> str:
        raise PermissionError(13, "Permission denied", str(config))

    monkeypatch.setattr(Path, "read_text", fail_to_read)

    with pytest.raises(ConfigurationError) as captured:
        load_config(config, display_name=config.name)

    assert str(config) not in str(captured.value)
    assert "Permission denied" in str(captured.value)
