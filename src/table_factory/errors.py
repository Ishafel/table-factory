"""Domain errors that are safe to show in command-line output."""


class TableFactoryError(Exception):
    """Base class for expected, user-actionable failures."""


class ConfigurationError(TableFactoryError):
    """The YAML configuration is missing or invalid."""


class DdlParseError(TableFactoryError):
    """A Hive DDL document cannot be represented by the parser."""


class OutputSafetyError(TableFactoryError):
    """An output operation would violate the path-safety contract."""
