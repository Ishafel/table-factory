from __future__ import annotations

from pathlib import Path

import pytest
from conftest import invoke_cli


@pytest.mark.parametrize("column_name", ("order.id", "order:id"))
def test_hive_columns_that_cannot_be_queried_are_rejected_before_write(
    tmp_path: Path,
    repository_config: Path,
    column_name: str,
) -> None:
    ddl = tmp_path / "invalid_column.sql"
    ddl.write_text(
        f"CREATE TABLE source_db.events (`{column_name}` BIGINT);",
        encoding="utf-8",
    )
    output = tmp_path / "output"

    result = invoke_cli(
        "generate",
        "--input",
        ddl,
        "--output",
        output,
        "--config",
        repository_config,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 2
    assert "source table source_db.events" in result.stderr
    assert f"Hive column '{column_name}'" in result.stderr
    assert "dot or colon" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("column_name", ("partition.day", "partition:day"))
def test_unqueryable_partition_column_names_are_also_rejected(
    tmp_path: Path,
    repository_config: Path,
    column_name: str,
) -> None:
    ddl = tmp_path / "invalid_partition_column.sql"
    ddl.write_text(
        f"CREATE TABLE source_db.events (id BIGINT) PARTITIONED BY (`{column_name}` STRING);",
        encoding="utf-8",
    )

    result = invoke_cli(
        "validate",
        "--input",
        ddl,
        "--config",
        repository_config,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 2
    assert f"Hive column '{column_name}'" in result.stderr
    assert "dot or colon" in result.stderr
