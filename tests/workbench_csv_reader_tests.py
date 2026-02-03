# tests/test_get_csv_data.py
import csv
import sys
import tempfile

import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).parent.parent))
from workbench_utils import (
    get_csv_data,
    WorkbenchCsvReader,
    WorkbenchCsvReaderException,
)


@pytest.fixture
def base_config(tmp_path):
    return {
        "input_csv": "input.csv",
        "input_dir": str(tmp_path),
        "delimiter": ",",
        "task": "create",
        "id_field": "node_id",
        "csv_headers": "names",
        "csv_start_row": 0,
        "csv_stop_row": None,
        "ignore_csv_columns": [],
        "subdelimiter": "|",
        "clean_csv_values_skip": [],
        "temp_dir": tempfile.gettempdir(),
    }


def write_csv(path: Path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def test_basic_csv_processing(tmp_path, base_config):
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id", "title"],
            ["1", "Test node"],
        ],
    )

    base_config["input_csv"] = "input.csv"

    csv_reader = WorkbenchCsvReader(base_config)
    reader = csv_reader.get_csv_data()

    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["node_id"] == "1"
    assert rows[0]["title"] == "Test node"


def test_commented_rows_are_skipped(tmp_path, base_config):
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id", "title"],
            ["#1", "Ignored"],
            ["2", "Kept"],
        ],
    )

    base_config["input_csv"] = "input.csv"

    csv_reader = WorkbenchCsvReader(base_config)
    reader = csv_reader.get_csv_data()
    rows = list(reader)

    assert len(rows) == 1
    assert rows[0]["node_id"] == "2"


def test_missing_csv_exits(base_config):
    with patch.object(sys, "exit") as exit_mock:
        WorkbenchCsvReader(base_config)
        exit_mock.assert_called_once()


def test_duplicate_headers_exit(tmp_path, base_config):
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id", "node_id"],
            ["1", "2"],
        ],
    )

    base_config["input_csv"] = "input.csv"

    with patch.object(sys, "exit") as exit_mock:
        csv_reader = WorkbenchCsvReader(base_config)
        rows = csv_reader.get_csv_data()
        list(rows)
        exit_mock.assert_called_once()


def test_missing_required_id_column_exits(tmp_path, base_config):
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["title"],
            ["Hello"],
        ],
    )

    base_config["input_csv"] = "input.csv"
    base_config["task"] = "create"

    with pytest.raises(WorkbenchCsvReaderException) as excinfo:
        csv_reader = WorkbenchCsvReader(base_config)
        data = csv_reader.get_csv_data()
        list(data)  # Generators lazy process the CSV on first access.
    assert (
        '"create" tasks require a "node_id" CSV column. Please check your input CSV file and try again.'
        in str(excinfo.value)
    )


def test_csv_field_templates_added(tmp_path, base_config):
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id"],
            ["1"],
        ],
    )

    base_config.update(
        {
            "input_csv": "input.csv",
            "csv_field_templates": [{"status": "published"}],
        }
    )

    csv_reader = WorkbenchCsvReader(base_config)
    reader = csv_reader.get_csv_data()
    rows = list(reader)

    assert rows[0]["status"] == "published"


def test_ignore_csv_columns(tmp_path, base_config):
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id", "temp"],
            ["1", "remove-me"],
        ],
    )

    base_config.update(
        {
            "input_csv": "input.csv",
            "ignore_csv_columns": ["temp"],
        }
    )

    csv_reader = WorkbenchCsvReader(base_config)
    reader = csv_reader.get_csv_data()
    rows = list(reader)

    assert "temp" not in rows[0]


def test_csv_rows_to_process_list(tmp_path, base_config):
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id"],
            ["1"],
            ["2"],
        ],
    )

    base_config.update(
        {
            "input_csv": "input.csv",
            "csv_rows_to_process": ["2"],
        }
    )

    csv_reader = WorkbenchCsvReader(base_config)
    reader = csv_reader.get_csv_data()
    rows = list(reader)

    assert len(rows) == 1
    assert rows[0]["node_id"] == "2"


@patch("workbench_utils.get_nid_from_url_alias", return_value="99")
def test_node_id_url_conversion(mock_alias, tmp_path, base_config):
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id"],
            ["http://example.com/node/99"],
        ],
    )

    base_config["input_csv"] = "input.csv"

    csv_reader = WorkbenchCsvReader(base_config)
    reader = csv_reader.get_csv_data()
    rows = list(reader)

    assert rows[0]["node_id"] == "99"
