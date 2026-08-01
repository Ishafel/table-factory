"""Strict version 3 configuration loading for table-factory."""

from __future__ import annotations

import re
import string
from collections.abc import Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from table_factory.errors import ConfigurationError

_SQL_NAMESPACE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_SAFE_IDENTIFIER_FRAGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*\Z")
_SAFE_TEMPLATE_LITERAL = re.compile(r"[A-Za-z0-9_]*\Z")
_SAFE_CHANGESET_LITERAL = re.compile(r"[A-Za-z0-9_.-]*\Z")
_SAFE_LOCATION = re.compile(r"[A-Za-z0-9:/?&=._%+{}-]+\Z")
_NAME_PLACEHOLDERS = frozenset({"replica", "source_database", "source_table"})
_CHANGESET_PLACEHOLDERS = frozenset(
    {"replica", "source_database", "source_table", "external_table"}
)
_LOCATION_PLACEHOLDERS = frozenset(
    {"subscription", "original_hive_database", "hive_table", "profile", "server"}
)
_GREENPLUM_IDENTIFIER_BYTES = 63
_CONFIG_VERSION = 3
_LEGACY_CONFIG_VERSIONS = frozenset({1, 2})
ARTIFACT_ROLES = (
    "01_hive_create_physical",
    "02_hive_insert",
    "03_greenplum_create_external",
    "03_greenplum_create_external_liquibase",
    "04_greenplum_create_physical",
    "05_greenplum_insert",
)
_ALL_ARTIFACT_ROLES = frozenset(ARTIFACT_ROLES)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""

    def construct_mapping(
        self,
        node: MappingNode,
        deep: bool = False,
    ) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found unhashable key",
                    key_node.start_mark,
                )
            if key in mapping:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found duplicate mapping key",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


@dataclass(frozen=True, slots=True)
class OutputConfig:
    include_source_comment: bool = True
    filename_separator: str = "__"
    enabled_artifacts: frozenset[str] = field(default_factory=lambda: _ALL_ARTIFACT_ROLES)

    def artifact_enabled(self, role: str) -> bool:
        """Return whether one validated artifact role should be emitted."""
        return role in self.enabled_artifacts


@dataclass(frozen=True, slots=True)
class HiveStorageConfig:
    format: str = "textfile"
    field_delimiter: str = ","
    escape_character: str = "\\"
    null_value: str = "\\N"


@dataclass(frozen=True, slots=True)
class HiveConfig:
    target_database: str = "target_hive_db"
    replica: str = "replica"
    physical_table_name_template: str = "{replica}_{source_table}_physical"
    insert_mode: str = "into"
    storage: HiveStorageConfig = field(default_factory=HiveStorageConfig)


@dataclass(frozen=True, slots=True)
class GreenplumDistributionConfig:
    mode: str = "random"


@dataclass(frozen=True, slots=True)
class GreenplumExternalFormatConfig:
    kind: str = "custom"
    formatter: str = "pxfwritable_import"


@dataclass(frozen=True, slots=True)
class GreenplumExternalLiquibaseConfig:
    author: str = "22643610"
    changeset_id_template: str = "stg-{source_table}_ext"
    function_name: str = "f_create_external_table"


@dataclass(frozen=True, slots=True)
class GreenplumExternalConfig:
    location_template: str = (
        "pxf://prx_{subscription}_{original_hive_database}.{hive_table}"
        "?PROFILE={profile}&SERVER={server}"
    )
    profile: str = "hive"
    server: str = "default"
    format: GreenplumExternalFormatConfig = field(default_factory=GreenplumExternalFormatConfig)
    liquibase: GreenplumExternalLiquibaseConfig = field(
        default_factory=GreenplumExternalLiquibaseConfig
    )


@dataclass(frozen=True, slots=True)
class GreenplumConfig:
    database: str = "target_gp_database"
    external_schema: str = "ext"
    physical_schema: str = "dwh"
    replica: str = "replica"
    subscription: str = "subscription"
    original_hive_database: str = "original_hive_database"
    external_table_name_template: str = "{replica}_{source_table}_ext"
    physical_table_name_template: str = "{replica}_{source_table}"
    distribution: GreenplumDistributionConfig = field(default_factory=GreenplumDistributionConfig)
    external: GreenplumExternalConfig = field(default_factory=GreenplumExternalConfig)


@dataclass(frozen=True, slots=True)
class FactoryConfig:
    """Validated settings for the source-to-target SQL workflow."""

    version: int = _CONFIG_VERSION
    output: OutputConfig = field(default_factory=OutputConfig)
    hive: HiveConfig = field(default_factory=HiveConfig)
    greenplum: GreenplumConfig = field(default_factory=GreenplumConfig)

    @property
    def include_source_comment(self) -> bool:
        """Compatibility convenience for artifact headers."""
        return self.output.include_source_comment

    @property
    def filename_separator(self) -> str:
        """Compatibility convenience for artifact filenames."""
        return self.output.filename_separator

    def as_dict(self) -> dict[str, object]:
        """Return the complete effective configuration without filesystem details."""
        return {
            "version": self.version,
            "output": {
                "include_source_comment": self.output.include_source_comment,
                "filename_separator": self.output.filename_separator,
                "artifacts": {role: self.output.artifact_enabled(role) for role in ARTIFACT_ROLES},
            },
            "hive": {
                "target_database": self.hive.target_database,
                "replica": self.hive.replica,
                "physical_table_name_template": (self.hive.physical_table_name_template),
                "insert_mode": self.hive.insert_mode,
                "storage": {
                    "format": self.hive.storage.format,
                    "field_delimiter": self.hive.storage.field_delimiter,
                    "escape_character": self.hive.storage.escape_character,
                    "null_value": self.hive.storage.null_value,
                },
            },
            "greenplum": {
                "database": self.greenplum.database,
                "external_schema": self.greenplum.external_schema,
                "physical_schema": self.greenplum.physical_schema,
                "replica": self.greenplum.replica,
                "subscription": self.greenplum.subscription,
                "original_hive_database": self.greenplum.original_hive_database,
                "external_table_name_template": (self.greenplum.external_table_name_template),
                "physical_table_name_template": (self.greenplum.physical_table_name_template),
                "distribution": {
                    "mode": self.greenplum.distribution.mode,
                },
                "external": {
                    "location_template": (self.greenplum.external.location_template),
                    "profile": self.greenplum.external.profile,
                    "server": self.greenplum.external.server,
                    "format": {
                        "kind": self.greenplum.external.format.kind,
                        "formatter": self.greenplum.external.format.formatter,
                    },
                    "liquibase": {
                        "author": self.greenplum.external.liquibase.author,
                        "changeset_id_template": (
                            self.greenplum.external.liquibase.changeset_id_template
                        ),
                        "function_name": self.greenplum.external.liquibase.function_name,
                    },
                },
            },
        }


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a YAML mapping")
    if any(not isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label} keys must be strings")
    return value


def _keys(
    value: dict[str, Any],
    *,
    label: str,
    allowed: frozenset[str],
    required: frozenset[str] | None = None,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"{label} contains unknown key: {unknown[0]}")
    required_keys = allowed if required is None else required
    missing = sorted(required_keys - set(value))
    if missing:
        raise ConfigurationError(f"{label} is missing required key: {missing[0]}")


def _string_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} must be a string")
    if not value:
        raise ConfigurationError(f"{label} must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError(f"{label} must not contain control characters")
    return value


def _namespace(
    value: object,
    label: str,
    *,
    max_utf8_bytes: int | None = None,
) -> str:
    result = _string_value(value, label)
    if _SQL_NAMESPACE.fullmatch(result) is None:
        raise ConfigurationError(
            f"{label} must be one unqualified SQL identifier using letters, digits and underscores"
        )
    if max_utf8_bytes is not None and len(result.encode("utf-8")) > max_utf8_bytes:
        raise ConfigurationError(f"{label} must contain at most {max_utf8_bytes} UTF-8 bytes")
    return result


def _ascii_identifier_fragment(value: object, label: str) -> str:
    result = _string_value(value, label)
    if _SAFE_IDENTIFIER_FRAGMENT.fullmatch(result) is None:
        raise ConfigurationError(
            f"{label} must be an ASCII identifier fragment starting with a letter, "
            "digit or underscore and containing only letters, digits, underscores and hyphens"
        )
    return result


def _template_fields(
    value: str,
    *,
    label: str,
    allowed: frozenset[str],
    literal_pattern: re.Pattern[str],
) -> tuple[str, ...]:
    fields: list[str] = []
    try:
        parsed = tuple(string.Formatter().parse(value))
    except ValueError as error:
        raise ConfigurationError(f"{label} is malformed: {error}") from None
    for literal, field_name, format_spec, conversion in parsed:
        if literal_pattern.fullmatch(literal) is None:
            raise ConfigurationError(f"{label} contains unsafe literal characters")
        if field_name is None:
            continue
        if field_name not in allowed:
            raise ConfigurationError(f"{label} contains unknown placeholder: {field_name}")
        if format_spec or conversion:
            raise ConfigurationError(f"{label} placeholders cannot use conversion or formatting")
        fields.append(field_name)
    return tuple(fields)


def _name_template(value: object, label: str) -> str:
    result = _string_value(value, label)
    _template_fields(
        result,
        label=label,
        allowed=_NAME_PLACEHOLDERS,
        literal_pattern=_SAFE_TEMPLATE_LITERAL,
    )
    if not result.replace("{source_database}", "db").replace("{source_table}", "table"):
        raise ConfigurationError(f"{label} must produce a non-empty table name")
    return result


def _changeset_id_template(value: object) -> str:
    label = "greenplum.external.liquibase.changeset_id_template"
    result = _string_value(value, label)
    _template_fields(
        result,
        label=label,
        allowed=_CHANGESET_PLACEHOLDERS,
        literal_pattern=_SAFE_CHANGESET_LITERAL,
    )
    return result


def _one_character(value: object, label: str) -> str:
    result = _string_value(value, label)
    if len(result) != 1:
        raise ConfigurationError(f"{label} must contain exactly one character")
    return result


def _storage_character(value: object, label: str) -> str:
    result = _one_character(value, label)
    if not result.isascii() or not result.isprintable():
        raise ConfigurationError(f"{label} must be one printable ASCII character")
    if result.isdigit():
        raise ConfigurationError(
            f"{label} must not be a digit because Hive interprets numeric delimiters as byte codes"
        )
    return result


def _load_output_artifacts(raw_value: object) -> frozenset[str]:
    label = "output.artifacts"
    raw = _mapping(raw_value, label)
    _keys(
        raw,
        label=label,
        allowed=_ALL_ARTIFACT_ROLES,
        required=frozenset(),
    )
    enabled = set(_ALL_ARTIFACT_ROLES)
    for role, value in raw.items():
        if not isinstance(value, bool):
            raise ConfigurationError(f"{label}.{role} must be a boolean")
        if not value:
            enabled.remove(role)
    return frozenset(enabled)


def _load_output(raw_value: object) -> OutputConfig:
    raw = _mapping(raw_value, "output")
    allowed = frozenset({"include_source_comment", "filename_separator", "artifacts"})
    required = frozenset({"include_source_comment", "filename_separator"})
    _keys(raw, label="output", allowed=allowed, required=required)
    include_source_comment = raw["include_source_comment"]
    if not isinstance(include_source_comment, bool):
        raise ConfigurationError("output.include_source_comment must be a boolean")
    separator = _string_value(raw["filename_separator"], "output.filename_separator")
    if len(separator) > 8 or any(character not in "._-" for character in separator):
        raise ConfigurationError(
            "output.filename_separator may contain 1-8 '.', '_' or '-' characters"
        )
    return OutputConfig(
        include_source_comment=include_source_comment,
        filename_separator=separator,
        enabled_artifacts=(
            _load_output_artifacts(raw["artifacts"]) if "artifacts" in raw else _ALL_ARTIFACT_ROLES
        ),
    )


def _load_storage(raw_value: object) -> HiveStorageConfig:
    raw = _mapping(raw_value, "hive.storage")
    allowed = frozenset({"format", "field_delimiter", "escape_character", "null_value"})
    _keys(raw, label="hive.storage", allowed=allowed)
    storage_format = _string_value(raw["format"], "hive.storage.format").lower()
    if storage_format != "textfile":
        raise ConfigurationError(
            "hive.storage.format must be 'textfile'; other formats are not supported end-to-end"
        )
    field_delimiter = _storage_character(
        raw["field_delimiter"],
        "hive.storage.field_delimiter",
    )
    escape_character = _storage_character(
        raw["escape_character"],
        "hive.storage.escape_character",
    )
    if field_delimiter == escape_character:
        raise ConfigurationError("hive.storage.field_delimiter and escape_character must differ")
    null_value = _string_value(raw["null_value"], "hive.storage.null_value")
    if len(null_value) > 32:
        raise ConfigurationError("hive.storage.null_value must contain at most 32 characters")
    if not null_value.isascii() or not null_value.isprintable():
        raise ConfigurationError("hive.storage.null_value must contain printable ASCII only")
    if field_delimiter in null_value:
        raise ConfigurationError(
            "hive.storage.null_value must not contain hive.storage.field_delimiter"
        )
    if escape_character not in null_value:
        raise ConfigurationError(
            "hive.storage.null_value must contain hive.storage.escape_character "
            "so the same non-null text remains distinguishable"
        )
    if null_value.endswith(escape_character):
        raise ConfigurationError(
            "hive.storage.null_value must not end with hive.storage.escape_character"
        )
    return HiveStorageConfig(
        format=storage_format,
        field_delimiter=field_delimiter,
        escape_character=escape_character,
        null_value=null_value,
    )


def _load_hive(raw_value: object) -> HiveConfig:
    raw = _mapping(raw_value, "hive")
    allowed = frozenset(
        {
            "target_database",
            "replica",
            "physical_table_name_template",
            "insert_mode",
            "storage",
        }
    )
    _keys(raw, label="hive", allowed=allowed)
    insert_mode = _string_value(raw["insert_mode"], "hive.insert_mode").lower()
    if insert_mode not in {"into", "overwrite"}:
        raise ConfigurationError("hive.insert_mode must be 'into' or 'overwrite'")
    return HiveConfig(
        target_database=_namespace(raw["target_database"], "hive.target_database"),
        replica=_ascii_identifier_fragment(raw["replica"], "hive.replica"),
        physical_table_name_template=_name_template(
            raw["physical_table_name_template"],
            "hive.physical_table_name_template",
        ),
        insert_mode=insert_mode,
        storage=_load_storage(raw["storage"]),
    )


def _load_distribution(raw_value: object) -> GreenplumDistributionConfig:
    raw = _mapping(raw_value, "greenplum.distribution")
    _keys(raw, label="greenplum.distribution", allowed=frozenset({"mode"}))
    mode = _string_value(raw["mode"], "greenplum.distribution.mode").lower()
    if mode != "random":
        raise ConfigurationError("greenplum.distribution.mode must be 'random'")
    return GreenplumDistributionConfig(mode=mode)


def _load_external_format(raw_value: object) -> GreenplumExternalFormatConfig:
    raw = _mapping(raw_value, "greenplum.external.format")
    allowed = frozenset({"kind", "formatter"})
    _keys(raw, label="greenplum.external.format", allowed=allowed)
    kind = _string_value(raw["kind"], "greenplum.external.format.kind").lower()
    formatter = _string_value(raw["formatter"], "greenplum.external.format.formatter").lower()
    if kind != "custom" or formatter != "pxfwritable_import":
        raise ConfigurationError(
            "greenplum.external.format must use kind 'custom' with formatter "
            "'pxfwritable_import' for textfile/Hive compatibility"
        )
    return GreenplumExternalFormatConfig(kind=kind, formatter=formatter)


def _load_external_liquibase(raw_value: object) -> GreenplumExternalLiquibaseConfig:
    label = "greenplum.external.liquibase"
    raw = _mapping(raw_value, label)
    allowed = frozenset({"author", "changeset_id_template", "function_name"})
    _keys(raw, label=label, allowed=allowed)
    author = _string_value(raw["author"], f"{label}.author")
    if _SAFE_TOKEN.fullmatch(author) is None:
        raise ConfigurationError(f"{label}.author contains unsafe characters")
    return GreenplumExternalLiquibaseConfig(
        author=author,
        changeset_id_template=_changeset_id_template(raw["changeset_id_template"]),
        function_name=_namespace(
            raw["function_name"],
            f"{label}.function_name",
            max_utf8_bytes=_GREENPLUM_IDENTIFIER_BYTES,
        ),
    )


def _location_template(value: object) -> str:
    label = "greenplum.external.location_template"
    result = _string_value(value, label)
    if _SAFE_LOCATION.fullmatch(result) is None:
        raise ConfigurationError(f"{label} contains unsafe URI characters")
    fields = _template_fields(
        result,
        label=label,
        allowed=_LOCATION_PLACEHOLDERS,
        literal_pattern=_SAFE_LOCATION,
    )
    missing = sorted(_LOCATION_PLACEHOLDERS - set(fields))
    if missing:
        raise ConfigurationError(f"{label} is missing required placeholder: {missing[0]}")
    if not result.lower().startswith("pxf://"):
        raise ConfigurationError(f"{label} must start with 'pxf://'")
    resource, separator, query = result.partition("?")
    expected_resource = "pxf://prx_{subscription}_{original_hive_database}.{hive_table}"
    expected_parameters = {
        "PROFILE={profile}",
        "SERVER={server}",
    }
    parameters = query.split("&") if separator else []
    if (
        resource != expected_resource
        or len(parameters) != len(expected_parameters)
        or set(parameters) != expected_parameters
    ):
        raise ConfigurationError(
            f"{label} must use {expected_resource!r} and only "
            "PROFILE={profile} and SERVER={server} query parameters"
        )
    return result


def _load_external(raw_value: object) -> GreenplumExternalConfig:
    raw = _mapping(raw_value, "greenplum.external")
    allowed = frozenset({"location_template", "profile", "server", "format", "liquibase"})
    _keys(raw, label="greenplum.external", allowed=allowed)
    requested_profile = _string_value(raw["profile"], "greenplum.external.profile")
    server = _string_value(raw["server"], "greenplum.external.server")
    if _SAFE_TOKEN.fullmatch(requested_profile) is None:
        raise ConfigurationError("greenplum.external.profile contains unsafe characters")
    if requested_profile.casefold() != "hive":
        raise ConfigurationError(
            "greenplum.external.profile must be 'hive' for the supported textfile/Hive workflow"
        )
    profile = "hive"
    if _SAFE_TOKEN.fullmatch(server) is None:
        raise ConfigurationError("greenplum.external.server contains unsafe characters")
    return GreenplumExternalConfig(
        location_template=_location_template(raw["location_template"]),
        profile=profile,
        server=server,
        format=_load_external_format(raw["format"]),
        liquibase=_load_external_liquibase(raw["liquibase"]),
    )


def _load_greenplum(raw_value: object) -> GreenplumConfig:
    raw = _mapping(raw_value, "greenplum")
    allowed = frozenset(
        {
            "database",
            "external_schema",
            "physical_schema",
            "replica",
            "subscription",
            "original_hive_database",
            "external_table_name_template",
            "physical_table_name_template",
            "distribution",
            "external",
        }
    )
    _keys(raw, label="greenplum", allowed=allowed)
    return GreenplumConfig(
        database=_namespace(
            raw["database"],
            "greenplum.database",
            max_utf8_bytes=_GREENPLUM_IDENTIFIER_BYTES,
        ),
        external_schema=_namespace(
            raw["external_schema"],
            "greenplum.external_schema",
            max_utf8_bytes=_GREENPLUM_IDENTIFIER_BYTES,
        ),
        physical_schema=_namespace(
            raw["physical_schema"],
            "greenplum.physical_schema",
            max_utf8_bytes=_GREENPLUM_IDENTIFIER_BYTES,
        ),
        replica=_ascii_identifier_fragment(raw["replica"], "greenplum.replica"),
        subscription=_ascii_identifier_fragment(
            raw["subscription"],
            "greenplum.subscription",
        ),
        original_hive_database=_namespace(
            raw["original_hive_database"],
            "greenplum.original_hive_database",
        ),
        external_table_name_template=_name_template(
            raw["external_table_name_template"],
            "greenplum.external_table_name_template",
        ),
        physical_table_name_template=_name_template(
            raw["physical_table_name_template"],
            "greenplum.physical_table_name_template",
        ),
        distribution=_load_distribution(raw["distribution"]),
        external=_load_external(raw["external"]),
    )


def load_config(path: Path, *, display_name: str) -> FactoryConfig:
    """Read and strictly validate a version 3 YAML configuration file."""
    if not path.is_file():
        raise ConfigurationError(f"configuration file does not exist: {display_name}")

    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as error:
        detail = error.strerror or "I/O error"
        raise ConfigurationError(f"cannot read configuration {display_name}: {detail}") from None
    except UnicodeError:
        raise ConfigurationError(
            f"cannot read configuration {display_name}: invalid UTF-8"
        ) from None

    try:
        raw_value = yaml.load(contents, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as error:
        problem = getattr(error, "problem", None) or "invalid YAML"
        mark = getattr(error, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ConfigurationError(
            f"cannot read configuration {display_name}: {problem}{location}"
        ) from None

    raw = _mapping(raw_value, "configuration")
    version = raw.get("version")
    if type(version) is int and version in _LEGACY_CONFIG_VERSIONS:
        raise ConfigurationError(
            f"configuration version {version} is no longer supported; "
            f"migrate to version {_CONFIG_VERSION}"
        )
    allowed = frozenset({"version", "output", "hive", "greenplum"})
    _keys(
        raw,
        label="configuration",
        allowed=allowed,
        required=frozenset({"version"}),
    )
    version = raw["version"]
    if type(version) is not int or version != _CONFIG_VERSION:
        raise ConfigurationError(f"configuration version must be {_CONFIG_VERSION}")
    _keys(raw, label="configuration", allowed=allowed)

    return FactoryConfig(
        version=_CONFIG_VERSION,
        output=_load_output(raw["output"]),
        hive=_load_hive(raw["hive"]),
        greenplum=_load_greenplum(raw["greenplum"]),
    )
