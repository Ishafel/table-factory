from __future__ import annotations

from pathlib import Path

import pytest

from table_factory.config import load_config
from table_factory.errors import ConfigurationError


def test_boolean_is_not_accepted_as_configuration_version(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("version: true\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="version must be 1"):
        load_config(config, display_name=config.name)


def test_configuration_io_error_does_not_expose_an_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "private configuration.yaml"
    config.write_text("version: 1\n", encoding="utf-8")

    def fail_to_read(_path: Path, *, encoding: str) -> str:
        raise PermissionError(13, "Permission denied", str(config))

    monkeypatch.setattr(Path, "read_text", fail_to_read)

    with pytest.raises(ConfigurationError) as captured:
        load_config(config, display_name=config.name)

    assert str(config) not in str(captured.value)
    assert "Permission denied" in str(captured.value)
