# tests/test_get_csv_data.py
import csv
import sys
import tempfile
from functools import partial

import pytest
from pathlib import Path
from unittest.mock import patch

from pytest_benchmark.plugin import benchmark

sys.path.append(str(Path(__file__).parent.parent))
from workbench_utils import (
    get_csv_data,
    WorkbenchCsvReader,
    WorkbenchCsvReaderException,
)


@pytest.fixture(params=["function", "class"])
def csv_reader_factory(request):
    """
    Switch between:
      - legacy function: get_csv_data(config)
      - new class: WorkbenchCsvReader(config).get_csv_data()
    """
    if request.param == "function":

        def _factory(config):
            return get_csv_data(config)

        _factory.is_class_based = False
        return _factory
    else:

        def _factory(config):
            reader = WorkbenchCsvReader(config)
            return reader.get_csv_data()

        _factory.is_class_based = True
        return _factory


@pytest.fixture
def base_config(tmp_path, csv_reader_factory):
    """
    Provides a base configuration dictionary for CSV processing tests.
    Adjusts configuration based on whether using the legacy function or new class.
    """

    config = {
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
    if not csv_reader_factory.is_class_based:
        config.update(
            {
                "host": "http://example.com",
                "http_max_retries": 3,
                "http_backoff_factor": 1,
                "http_retry_on_status_codes": [500, 502, 503, 504],
                "http_retry_allowed_methods": [
                    "HEAD",
                    "GET",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                ],
                "secure_ssl_only": True,
                "username": "admin",
                "password": "secret",
                "check": False,
                "user_agent": "workbench-client/1.0",
                "log_request_url": False,
                "log_headers": False,
                "allow_redirects": True,
                "log_response_body": False,
                "log_response_status_code": False,
                "log_response_time_sample": False,
                "log_response_time": False,
            }
        )
    return config


def write_csv(path: Path, rows):
    """Helper function to write rows to a CSV file."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def process_csv(factory, config):
    """Helper function to process CSV using the provided factory and config."""
    reader = factory(config)
    return list(reader)


def test_basic_create_csv_processing(
    tmp_path, base_config, csv_reader_factory, benchmark
):
    """Test basic CSV processing for a 'create' task."""
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [["node_id", "title"], ["1", "Test node"], ["2", "Another node"]],
    )

    base_config["input_csv"] = "input.csv"

    rows = benchmark(partial(process_csv, csv_reader_factory, base_config))
    assert len(rows) == 2
    assert rows[0]["node_id"] == "1"
    assert rows[0]["title"] == "Test node"
    assert rows[1]["node_id"] == "2"
    assert rows[1]["title"] == "Another node"


def test_basic_update_csv_processing(
    tmp_path, base_config, csv_reader_factory, benchmark
):
    """Test basic CSV processing for an 'update' task."""
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [["node_id", "title"], ["1", "Test node"], ["2", "Another node"]],
    )
    base_config["task"] = "update"
    base_config["input_csv"] = "input.csv"

    rows = benchmark(partial(process_csv, csv_reader_factory, base_config))
    assert len(rows) == 2
    assert rows[0]["node_id"] == "1"
    assert rows[0]["title"] == "Test node"
    assert rows[1]["node_id"] == "2"
    assert rows[1]["title"] == "Another node"


def test_commented_rows_are_skipped(
    tmp_path, base_config, csv_reader_factory, benchmark
):
    """Test that commented rows in the CSV are skipped."""
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id", "title"],
            ["#1", "Ignored"],
            ["2", "Kept"],
        ],
    )

    if not csv_reader_factory.is_class_based:
        pytest.skip(
            "Legacy function throws an unexpected JSONDecodeError when trying to resolve the URL #1."
        )

    base_config["input_csv"] = "input.csv"

    rows = benchmark(partial(process_csv, csv_reader_factory, base_config))

    assert len(rows) == 1
    assert rows[0]["node_id"] == "2"


def test_missing_csv_exits(base_config, csv_reader_factory):
    """Test that processing exits when the input CSV file is missing."""
    if not csv_reader_factory.is_class_based:
        pytest.skip(
            "Legacy function throws an unexpected FileNotFoundError when trying to open the missing file."
        )

    with patch.object(sys, "exit") as exit_mock:
        csv_reader_factory(base_config)
        exit_mock.assert_called_once()


def test_duplicate_headers_exit(tmp_path, base_config, csv_reader_factory):
    """Test that processing exits when the CSV has duplicate headers."""
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id", "node_id"],
            ["1", "2"],
        ],
    )

    base_config["input_csv"] = "input.csv"

    if csv_reader_factory.is_class_based:
        with pytest.raises(WorkbenchCsvReaderException) as excinfo:
            list(csv_reader_factory(base_config))
        assert "Error: CSV has duplicate header names - node_id" in str(excinfo.value)
    else:
        with patch.object(sys, "exit") as exit_mock:
            list(csv_reader_factory(base_config))
            exit_mock.assert_called_once()


def test_missing_required_id_column_exits(tmp_path, base_config, csv_reader_factory):
    """Test that processing exits when the required ID column is missing."""
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

    if csv_reader_factory.is_class_based:
        with pytest.raises(WorkbenchCsvReaderException) as excinfo:
            list(csv_reader_factory(base_config))
        assert (
            '"create" tasks require a "node_id" CSV column. Please check your input CSV file and try again.'
            in str(excinfo.value)
        )
    else:
        pytest.skip("Legacy function throws an unexpected KeyError.")
        # legacy function
        with patch.object(sys, "exit") as exit_mock:
            list(csv_reader_factory(base_config))
            exit_mock.assert_called_once()


def test_csv_field_templates_added(
    tmp_path, base_config, csv_reader_factory, benchmark
):
    """Test that CSV field templates are added to each row."""
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [["node_id"], ["1"], ["2"]],
    )

    base_config.update(
        {
            "input_csv": "input.csv",
            "csv_field_templates": [{"status": "published"}],
        }
    )

    rows = benchmark(partial(process_csv, csv_reader_factory, base_config))

    assert len(rows) == 2
    assert rows[0]["node_id"] == "1"
    assert rows[0]["status"] == "published"
    assert rows[1]["node_id"] == "2"
    assert rows[1]["status"] == "published"


def test_ignore_csv_columns(tmp_path, base_config, csv_reader_factory, benchmark):
    """Test that specified CSV columns are ignored during processing."""
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id", "title", "temp"],
            ["1", "Node 1", "remove-me"],
        ],
    )

    base_config.update(
        {
            "input_csv": "input.csv",
            "ignore_csv_columns": ["temp"],
        }
    )

    rows = benchmark(partial(process_csv, csv_reader_factory, base_config))

    assert len(rows) == 1
    assert rows[0]["node_id"] == "1"
    assert rows[0]["title"] == "Node 1"
    assert "temp" not in rows[0]


def test_csv_rows_to_process_list(tmp_path, base_config, csv_reader_factory, benchmark):
    """Test that only specified CSV rows are processed."""
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id"],
            ["1"],
            ["2"],
            ["3"],
        ],
    )

    base_config.update(
        {
            "input_csv": "input.csv",
            "csv_rows_to_process": ["2"],
        }
    )

    rows = benchmark(partial(process_csv, csv_reader_factory, base_config))

    assert len(rows) == 1
    assert rows[0]["node_id"] == "2"


@patch("workbench_utils.get_nid_from_url_alias", return_value="99")
def test_node_id_url_conversion(
    mock_alias, tmp_path, base_config, csv_reader_factory, benchmark
):
    """Test that node IDs provided as URLs are converted correctly."""
    input_csv = tmp_path / "input.csv"
    write_csv(
        input_csv,
        [
            ["node_id"],
            ["http://example.com/node/99"],
        ],
    )

    base_config["input_csv"] = "input.csv"

    rows = benchmark(partial(process_csv, csv_reader_factory, base_config))

    assert rows[0]["node_id"] == "99"
