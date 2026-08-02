"""Domain errors that are safe to show in command-line output."""


class TableFactoryError(Exception):
    """Base class for expected, user-actionable failures."""


class ConfigurationError(TableFactoryError):
    """The YAML configuration is missing or invalid."""


class DdlParseError(TableFactoryError):
    """A Hive DDL document cannot be represented by the parser."""


class DdlTableLimitError(DdlParseError):
    """A DDL document exceeds the caller's remaining table budget."""


class DdlColumnLimitError(DdlParseError):
    """A DDL document exceeds the caller's remaining column budget."""


class SemanticValidationError(TableFactoryError):
    """A parsed table cannot be transformed safely with the active config."""


class TypeMappingError(SemanticValidationError):
    """A Hive column has no reliable Greenplum type mapping."""


class OutputSafetyError(TableFactoryError):
    """An output operation would violate the path-safety contract."""
