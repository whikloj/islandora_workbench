import copy
import csv
import hashlib
import json
import logging
import os
import sys
import tempfile
from typing import Union

import openpyxl
import requests

from islandora_workbench.utils import get_config_file_identifier

"""This file contains functions for supporting the reading and writing of preprocessed CSV files and their associated metadata sidecar files."""

def read_meta(meta_path: str) -> Union[dict, None]:
    """Read the metadata from a sidecar file.

    :param meta_path: str - The path to the sidecar file.
    :return: dict|None - The metadata, or None if the file does not exist or is unreadable.
    """
    if os.path.exists(meta_path) and os.path.isfile(meta_path):
        try:
            with open(meta_path, "r") as meta_f:
                return json.load(meta_f)
        except (OSError, json.JSONDecodeError):
            return None
    return None


def get_csv_cache_path(config: dict) -> str:
    """
    Get the path of the preprocessed CSV file.

    :param config: dict - The configuration settings defined by workbench_config.get_config().
    :return: str - The path to the preprocessed file.
    """
    temp_dir = config.get("temp_dir", tempfile.gettempdir())
    return os.path.join(temp_dir, os.path.basename(get_source_file_path(config))) + ".preprocessed"

def get_config_hash_path(config: dict) -> str:
    """
    Get the path of the hash file for the preprocessed CSV file.

    :param config: dict - The configuration settings defined by workbench_config.get_config().
    :return: str - The path to the hash file.
    """
    return get_csv_cache_path(config) + ".hash"


def get_source_file_path(config: dict, csv_file_target: str = "node_fields") -> str:
    """Resolve the actual source file path for the CSV file.

    :param config: dict - The configuration settings defined by workbench_config.get_config().
    :param csv_file_target: str - The target for the CSV file.
    :return: str - The resolved source file path.
    :raise FileNotFoundError: If the source file path cannot be resolved.
    """
    local_file_path = get_csv_cache_path(config)
    if csv_file_target == "node_fields":
        local_file_path = config["input_csv"]

    if os.path.isabs(local_file_path) and os.path.exists(local_file_path) and os.path.isfile(local_file_path):
        return local_file_path
    elif local_file_path.startswith("http") or local_file_path.endswith(".xlsx"):
        # For Google Sheets, etc.
        extracted_path = get_extracted_csv_file_path(config)
        # Always fetch a fresh copy.
        if os.path.exists(extracted_path):
            os.remove(extracted_path)
        if local_file_path.startswith("http"):
            get_csv_from_google_sheet(config)
        elif local_file_path.endswith(".xlsx"):
            get_csv_from_excel(config)
        return extracted_path
    elif os.path.exists(os.path.join(config["input_dir"], local_file_path)) and os.path.isfile(
            os.path.join(config["input_dir"], local_file_path)
            ):
        return os.path.join(config["input_dir"], local_file_path)

    raise FileNotFoundError("Unable to resolve source file path for CSV file: " + local_file_path)

def is_cache_valid(config: dict) -> bool:
    """
    Check if cached preprocessed file is still valid.

    NOTE: This function is not used in the current implementation, but it may be useful for future enhancements.

    Cache is valid if:
    1. Cache file exists
    2. Cache file is newer than source file
    3. Cache file is not empty
    4. Cache file was made with the same settings as current config excluding the "check" key.

    :param config: The path to the cache file.
    :return: bool - True if cache is valid, False otherwise.
    """
    csv_cache_path = get_csv_cache_path(config)
    csv_source_path = get_source_file_path(config)
    if not os.path.exists(csv_cache_path) or not os.path.isfile(csv_cache_path):
        return False

    if not os.path.exists(csv_source_path) or not os.path.isfile(csv_source_path):
        return False

    # Check if cache is newer than source
    cache_mtime = os.path.getmtime(csv_cache_path)
    source_mtime = os.path.getmtime(csv_source_path)

    if cache_mtime <= source_mtime:
        return False

    # Check if cache is not empty
    if os.path.getsize(csv_cache_path) == 0:
        return False

    # Check that the cache was made with the same settings.
    try:
        with open(get_config_hash_path(config), "r") as hash_file:
            if hash_file.read().strip() != fingerprint(config):
                return False
    except OSError:
        return False

    return True

def write_meta_atomic(meta_path: str, meta: dict):
    """Write the metadata to a sidecar file atomically.

    :param meta_path: str - The path to the sidecar file.
    :param meta: dict - The metadata to write.
    """
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp_meta_f:
        temp_meta_path = temp_meta_f.name
        json.dump(meta, temp_meta_f)
    os.replace(temp_meta_path, meta_path)


def get_extracted_csv_file_path(config: dict) -> str:
    """For secondary tasks were input is either a Google Sheet or an Excel file,
    get the path to the extracted CSV.
    :param config The configuration settings defined by workbench_config.get_config().
    :return string: A file path with the current config file's unique ID appended to it.
    :raise ValueError if the input_csv value is not a http(s) URL or an Excel file path.
    """
    if config["input_csv"].startswith("http"):
        exported_csv_filename = config["google_sheets_csv_filename"]
    elif config["input_csv"].endswith("xlsx"):
        exported_csv_filename = config["excel_csv_filename"]
    else:
        raise ValueError(f"Invalid input_csv value: {config['input_csv']}. Must be a Google Sheet URL or an Excel file path.")

    if os.environ.get("ISLANDORA_WORKBENCH_SECONDARY_TASKS") is not None:
        secondary_tasks = json.loads(os.environ["ISLANDORA_WORKBENCH_SECONDARY_TASKS"])
        if os.path.abspath(config["current_config_file_path"]) in secondary_tasks:
            config_file_id = get_config_file_identifier(config)
            exported_csv_filename = exported_csv_filename + "." + config_file_id

    return os.path.join(config["temp_dir"], exported_csv_filename)


def get_csv_from_excel(config: dict) -> None:
    """Read the input Excel 2010 (or later) file and write it out as CSV.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    """
    if os.path.isabs(config["input_csv"]):
        input_excel_path = config["input_csv"]
    else:
        input_excel_path = os.path.join(config["input_dir"], config["input_csv"])

    if not os.path.exists(input_excel_path):
        message = "Error: Excel file " + input_excel_path + " not found."
        logging.error(message)
        sys.exit(message)

    wb = openpyxl.load_workbook(filename=input_excel_path)
    ws = wb[config["excel_worksheet"]]

    headers = []
    header_row = ws[1]
    ws.delete_rows(0)
    for header_cell in header_row:
        headers.append(header_cell.value)

    records = []
    for row in ws:
        record = {}
        for x in range(len(header_row)):
            if headers[x] is not None and row[x] is not None:
                record[headers[x]] = row[x].value
        records.append(record)

    if os.environ.get("ISLANDORA_WORKBENCH_SECONDARY_TASKS") is not None:
        secondary_tasks = json.loads(os.environ["ISLANDORA_WORKBENCH_SECONDARY_TASKS"])
        if os.path.abspath(config["current_config_file_path"]) in secondary_tasks:
            config_file_id = get_config_file_identifier(config)
            exported_csv_path = os.path.join(
                config["temp_dir"], config["excel_csv_filename"] + "." + config_file_id
            )
        else:
            exported_csv_path = os.path.join(
                config["temp_dir"], config["excel_csv_filename"]
            )
    else:
        exported_csv_path = os.path.join(
            config["temp_dir"], config["excel_csv_filename"]
        )

    with open(
        exported_csv_path, "w+", newline="", encoding="utf-8"
    ) as csv_writer_file_handle:
        csv_writer = csv.DictWriter(csv_writer_file_handle, fieldnames=headers)
        csv_writer.writeheader()
        for record in records:
            if (config["id_field"] in record or "node_id" in record) and record[
                config["id_field"]
            ] is not None:
                csv_writer.writerow(record)


def get_csv_from_google_sheet(config: dict) -> None:
    """Read the input Google Sheet as a Google Sheet URL and write it out as CSV.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    """
    url_parts = config["input_csv"].split("/")
    url_parts[6] = "export?gid=" + str(config["google_sheets_gid"]) + "&format=csv"
    csv_url = "/".join(url_parts)
    response = requests.get(url=csv_url, allow_redirects=True)

    if response.status_code == 404:
        message = (
            "Workbench cannot find the Google spreadsheet at "
            + config["input_csv"]
            + ". Please check the URL."
        )
        logging.error(message)
        sys.exit("Error: " + message)

    # Sheets that aren't publicly readable return a 302 and then a 200 with a bunch of HTML for humans to look at.
    if response.content.strip().startswith(b"<!doctype html"):
        message = (
            "The Google spreadsheet at "
            + config["input_csv"]
            + ' is not accessible.\nPlease check its "Share" settings.'
        )
        logging.error(message)
        sys.exit("Error: " + message)

    if os.environ.get("ISLANDORA_WORKBENCH_SECONDARY_TASKS") is not None:
        secondary_tasks = json.loads(os.environ["ISLANDORA_WORKBENCH_SECONDARY_TASKS"])
        if os.path.abspath(config["current_config_file_path"]) in secondary_tasks:
            config_file_id = get_config_file_identifier(config)
            exported_csv_path = os.path.join(
                config["temp_dir"],
                config["google_sheets_csv_filename"] + "." + config_file_id,
            )
        else:
            exported_csv_path = os.path.join(
                config["temp_dir"], config["google_sheets_csv_filename"]
            )
    else:
        exported_csv_path = os.path.join(
            config["temp_dir"], config["google_sheets_csv_filename"]
        )

    open(exported_csv_path, "wb+").write(response.content)


def fingerprint(config: dict, csv_file_target: str = "node_fields") -> str:
    """Generate a hash based on:
        - Source file path
        - The csv_file_target
        - The entire current configuration, sorted to avoid ordering issues

    It is stored next to the preprocessed CSV file with the same filename and ".sidecar.json" appended.
    (i.e. temp/some_file_name.csv.preprocessed will have temp/some_file_name.csv.preprocessed.sidecar.json).

    :param config: dict - The configuration settings defined by workbench_config.get_config().
    :param csv_file_target: str - The target for the CSV file.
    :return: str - The hash.
    """
    config_keys_to_ignore = ["check", "check_lock_file_path", "secondary_tasks", "contactsheet", "contact_sheet_output_dir"]
    config_copy = copy.deepcopy(config)
    # Remove keys that should not be included in the hash calculation
    for key in config_keys_to_ignore:
        config_copy.pop(key, None)

    effective_config = {"file_path": get_source_file_path(config), "csv_file_target": csv_file_target,
        **config_copy, }
    config_json = json.dumps(
        effective_config, sort_keys=True, default=str, separators=(",", ":"), )
    config_hash = hashlib.sha256(config_json.encode()).hexdigest()
    return config_hash
