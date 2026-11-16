"""Utility functions for Islandora Workbench."""

import os
import sys
import json
from argparse import Namespace
from csv import DictReader
import csv
from typing import OrderedDict, Union

import string
import re
import copy
import logging
import datetime
import requests
from requests.packages.urllib3.util.retry import Retry
import collections
import urllib.parse
from ruamel.yaml import YAML
import itertools
import http.client
from rich.traceback import install

from islandora_workbench.drupal import ping_islandora
from islandora_workbench.data_io.csv.csv_reader import CsvReader
from islandora_workbench.data_io.csv.csv_process import clean_csv_values, apply_csv_value_templates, \
    replace_field_labels_with_names
from islandora_workbench.data_io.csv.csv_cache import get_extracted_csv_file_path, get_csv_from_excel, \
    get_csv_from_google_sheet
from islandora_workbench.data_io.output_csv import write_to_output_csv
from islandora_workbench.data_io.rollback import write_rollback_node_id
from islandora_workbench.drupal.client import get_term_id_from_uri, get_mid_from_media_url_alias
from islandora_workbench.drupal.media import get_node_media_ids
from islandora_workbench.utils import value_is_numeric
from islandora_workbench.utils import get_remote_file_extension, get_preprocessed_file_path

install()

# Set some global variables.
yaml = YAML()

EXECUTION_START_TIME = datetime.datetime.now()
# Workaround for https://github.com/mjordan/islandora_workbench/issues/360.
http.client._MAXHEADERS = 10000
http_response_times = []
# Global lists of terms to reduce queries to Drupal.
checked_terms = list()
newly_created_terms = list()
# These are the Drupal field names on the standard types of media.
file_fields = [
    "field_media_file",
    "field_media_image",
    "field_media_document",
    "field_media_audio_file",
    "field_media_video_file",
]
commented_out_input_csv_rows_present = False
# Hold the version number to avoid requesting it multiple times.
workbench_integration_module_version = None


def set_media_type(
    config: dict, filepath: str, file_fieldname: str, csv_row: dict
) -> str:
    """Using either the 'media_type' or 'media_types_override' configuration
    setting, determine which media bundle type to use.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    filepath: string
        The value of the CSV 'file' column.
    file_fieldname: string
         The name of the CSV column containing the filename (usually 'file'). None if the file
         isn't in a CSV field (e.g., when config['paged_content_from_directories'] or
         config['paged_content_from_directories_parents_exist'] is True).
    csv_row : dict
         The CSV row for the current item.
    Returns
    -------
    string
        A string naming the configured media type, e.g. 'image'.
    """
    if "media_type" in config:
        return config["media_type"]

    if config["media_type_by_media_use"] and len(config["media_type_by_media_use"]) > 0 and "additional_files" in config and len(config["additional_files"]) > 0 and file_fieldname in config["additional_files"]:
        additional_files = get_additional_files_config(config)
        media_url = additional_files.get(file_fieldname, None)
        if media_url is not None and media_url != "" and media_url in config["media_type_by_media_use"]:
            return config["media_type_by_media_use"][media_url]

    # Determine if the incoming filepath matches a registered eEmbed media type.
    oembed_media_type = get_oembed_url_media_type(config, filepath)
    if oembed_media_type is not None:
        return oembed_media_type

    if file_fieldname is not None and filepath.strip().startswith("http"):
        preprocessed_file_path = get_preprocessed_file_path(
            config, file_fieldname, csv_row
        )
        filename = preprocessed_file_path.split("/")[-1]
        extension = filename.split(".")[-1]
        extension_with_dot = "." + extension
    else:
        extension_with_dot = os.path.splitext(filepath)[-1]

    extension = extension_with_dot[1:]
    normalized_extension = extension.lower()
    media_type = "file"
    for types in config["media_types"]:
        for typ, extensions in types.items():
            if normalized_extension in extensions:
                media_type = typ
    if "media_types_override" in config:
        for override in config["media_types_override"]:
            for typ, extensions in override.items():
                if normalized_extension in extensions:
                    media_type = typ

    # If extension isn't in one of the lists, default to 'file' bundle.
    return media_type


def get_oembed_url_media_type(config: dict, filepath: str) -> Union[str, None]:
    """Since oEmbed remote media (e.g. remove video) don't have extensions, which we
    use to detect the media type of local files, we use remote URL patterns to
    detect if the value of the 'file' columns is an oEmbed media.

    Parameters
    ----------
    config : dict
       The configuration settings defined by workbench_config.get_config().
    filepath: string
       The value of the CSV 'file' column.
    Returns
    -------
    mtype : str|None
       A string naming the detected media type, e.g. 'remote_video', or None
       if the filepath does not start with a configured provider URL.
    """
    for oembed_provider in config["oembed_providers"]:
        for mtype, provider_urls in oembed_provider.items():
            for provider_url in provider_urls:
                if filepath.startswith(provider_url):
                    return mtype

    return None


def get_oembed_media_types(config: dict) -> list:
    """Get a list of the registered oEmbed media types from config.

    Parameters
    ----------
    config : dict
           The configuration settings defined by workbench_config.get_config().

    Returns
    -------
    media_types : list
        A list with the configured allowed oEmbed media type(s), e.g. ['remote_video'].
    """

    media_types = list()
    for omt in config["oembed_providers"]:
        keys = list(omt.keys())
        media_types.append(keys[0])
    return media_types


def set_model_from_extension(file_name: str, config: dict) -> Union[None, str, dict]:
    """Using configuration options, determine which Islandora Model value
    to assign to nodes created from files. Options are either a single model
    or a set of mappings from file extension to Islandora Model term ID.

    Parameters
    ----------
    file_name : str
        Filename that will be checked to determine Islandora Model value(s).
    config : dict
        The configuration settings defined by workbench_config.get_config().


    Returns
    -------
    None|str|dict
        None is returned if 'task' is not set to 'create_from_files'.

        str is returned if 'model' config value is set, a single model term ID is str returned.

        dict is returned if 'models' config value is set, a dict with a mapping of URIs or Islandora Model term ID(s)
        to file extension(s) is returned.
        TODO: There is a possibility in "models" we miss the two return statements. There should be a default.
    """
    if config["task"] != "create_from_files":
        return None

    if "model" in config:
        return config["model"]

    extension_with_dot = os.path.splitext(file_name)[1]
    extension = extension_with_dot[1:]
    normalized_extension = extension.lower()
    for model_tids in config["models"]:
        for tid, extensions in model_tids.items():
            if str(tid).startswith("http"):
                tid = get_term_id_from_uri(config, tid)
            if normalized_extension in extensions:
                return tid
            # If the file's extension is not listed in the config,
            # We use the term ID that contains an empty extension.
            if "" in extensions:
                return tid


def extract_media_id(config: dict, media_csv_row: dict) -> Union[str, None]:
    """Extract the media entity's ID from the CSV row.

    Parameters
    ----------
    config : dict
        The global configuration object.
    media_csv_row : OrderedDict
        The CSV row containing the media entity's field names and values.

    Returns
    -------
    str|None
        The media entity's ID if it could be extracted from the CSV row and is valid, otherwise None.
    """
    if "media_id" not in media_csv_row:  # Media ID column is missing
        logging.error("Media ID column missing in CSV file.")
        return None

    if not media_csv_row["media_id"]:  # Media ID column is present but empty
        logging.error("Row with empty media_id column detected in CSV file.")
        return None

    # If media ID is not numeric, assume it is a media URL alias.
    if not value_is_numeric(media_csv_row["media_id"]):
        # Note that this function returns False if the media URL alias does not exist.
        media_id = get_mid_from_media_url_alias(config, media_csv_row["media_id"])
        # Media URL alias does not exist.
        if media_id is False:
            logging.error(
                "Media URL alias %s does not exist.", media_csv_row["media_id"]
            )
            return None
        else:
            return str(media_id)
    # If media ID is numeric, use it as is, if it is a valid media ID
    else:
        media_response_code = ping_media(config, media_csv_row["media_id"])
        if media_response_code is not True:
            logging.error("Media ID %s does not exist.", media_csv_row["media_id"])
            return None
        # If media ID exists, use it as is (since this is a string)
        else:
            return media_csv_row["media_id"]


def ping_remote_file(config: dict, url: str) -> int:
    """Ping remote file, but logging, exiting, etc. happens in caller, except on requests error.
    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    url : str
        URL of remote file to be pinged.
    Returns
    -------
    int: Status code of the remote URL
    """
    headers = {"User-Agent": config["user_agent"]}
    cookies = {config["remote_file_cookie_name"]: config["remote_file_cookie_value"]}

    sections = urllib.parse.urlparse(url)
    try:
        response = requests.head(
            url,
            allow_redirects=True,
            verify=config["secure_ssl_only"],
            headers=headers,
            cookies=cookies,
        )
        return response.status_code
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
        message = (
            "Workbench " + "timed out trying to reach "
            if isinstance(error, requests.exceptions.Timeout)
            else "cannot connect to "
            + sections.netloc
            + " while connecting to "
            + url
            + ". Please verify that URL and check your network connection."
        )
        logging.error(message)
        logging.error(error)
        sys.exit("Error: " + message)


def get_registered_media_extensions(
    config: dict, media_bundle: str, field_name_filter: Union[str, None] = None
) -> dict:
    """For the given media bundle, gets a list of file extensions registered in Drupal's
    "Allowed file extensions" configuration for each field that has this setting.
    """
    """Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        bundle_type : string
            The (node) content type, the vocabulary name, or the media type (image',
            'document', 'audio', 'video', 'file', etc.).
        field_name_filter: string
            If not None, filter the return value to just that field; if the field is
            not a key in the return dict, return an empty dictionary.
        Returns
        -------
        dict
            A dictionary with one key per media bundle field name that has registered extensions.
            Each key has as its value a list of file extensions, without leading periods,
            registered for those fields in Drupal. All extensions are lower case.
    """
    registered_extensions = dict()
    media_field_definitions = get_field_definitions(config, "media", media_bundle)
    for field_name, field_def in media_field_definitions.items():
        if "file_extensions" in field_def:
            registered_extensions[field_name] = re.split(
                r"\s+", field_def["file_extensions"]
            )
            for i in range(len(registered_extensions[field_name])):
                registered_extensions[field_name][i] = registered_extensions[
                    field_name
                ][i].lower()

    if field_name_filter is not None:
        return {
            k: v for k, v in registered_extensions.items() if k == field_name_filter
        }
    else:
        return registered_extensions


def check_input_for_create_from_files(config: dict, args: Namespace) -> None:
    """Validate the config file and input data if task is 'create_from_files'.
    :param config: dict - The configuration settings defined by workbench_config.get_config().
    :param args: Namespace - The argparse Namespace object created in workbench.py.
    """
    if config["task"] != "create_from_files":
        message = 'Your task must be "create_from_files".'
        logging.error(message)
        sys.exit("Error: " + message)

    logging.info(
        'Starting configuration check for "%s" task using config file %s.',
        config["task"],
        args.config,
    )

    ping_islandora(config, print_message=False)

    config_keys = list(config.keys())
    unwanted_in_create_from_files = [
        "check",
        "delimiter",
        "subdelimiter",
        "allow_missing_files",
        "paged_content_from_directories",
        "paged_content_from_directories_parents_exist",
        "delete_media_with_nodes",
        "allow_adding_terms",
    ]
    for option in unwanted_in_create_from_files:
        if option in config_keys:
            config_keys.remove(option)
    joiner = ", "
    # Check for presence of required config keys.
    create_required_options = ["task", "host", "username", "password"]
    for create_required_option in create_required_options:
        if create_required_option not in config_keys:
            message = (
                "Please check your config file for required values: "
                + joiner.join(create_required_options)
                + "."
            )
            logging.error(message)
            sys.exit("Error: " + message)

    # Check existence of input directory.
    if os.path.exists(config["input_dir"]):
        message = 'OK, input directory "' + config["input_dir"] + '" found.'
        print(message)
        logging.info(message)
    else:
        message = 'Input directory "' + config["input_dir"] + '"" not found.'
        logging.error(message)
        sys.exit("Error: " + message)

    # Validate length of 'title'.
    files = os.listdir(config["input_dir"])
    for file_name in files:
        filename_without_extension = os.path.splitext(file_name)[0]
        if len(filename_without_extension) > int(config["max_node_title_length"]):
            message = (
                'The filename "'
                + filename_without_extension
                + "\" exceeds Drupal's maximum length of "
                + config["max_node_title_length"]
                + " characters and cannot be used for a node title."
            )
            logging.error(message)
            sys.exit("Error: " + message)

    # Check that either 'model' or 'models' are present in the config file.
    if "model" not in config and "models" not in config:
        message = 'You must include either the "model" or "models" option in your configuration.'
        logging.error(message)
        sys.exit("Error: " + message)

    # If nothing has failed by now, exit with a positive message.
    if config["perform_soft_checks"] is True:
        always_review_log_message = ""
    else:
        always_review_log_message = (
            " However, you should review your Workbench log after running --check."
        )
    config_and_data_appear_to_be_valid_message = (
        f"Configuration and input data appear to be valid.{always_review_log_message}"
    )
    print(config_and_data_appear_to_be_valid_message)
    if config["perform_soft_checks"] is True:
        print(
            'Warning: "perform_soft_checks" is enabled so you need to review your log for errors despite the "OK" reports above.'
        )
    logging.info(
        'Configuration checked for "%s" task using config file %s, no problems found.',
        config["task"],
        args.config,
    )
    sys.exit(0)


# TODO: This does not seem to be called anywhere. Is it needed?
def get_target_ids(node_field_values):
    """Get the target IDs of all entities in a field."""
    target_ids = []
    for target in node_field_values:
        target_ids.append(target["target_id"])
    return target_ids


def get_additional_files_config(config: dict) -> dict:
    """Converts values in 'additional_files' config setting to a simple
    dictionary for easy access.
    """
    additional_files_entries = dict()
    if "additional_files" in config and len(config["additional_files"]) > 0:
        for additional_files_entry in config["additional_files"]:
            for (
                additional_file_field,
                additional_file_media_use_tid,
            ) in additional_files_entry.items():
                additional_files_entries[additional_file_field] = (
                    additional_file_media_use_tid
                )
    return additional_files_entries


# TODO: this function and execute_bootstrap_script(), execute_shutdown_script(), execute_entity_post_task_script() are very similar.
# Could they be combined and accept *args which they pass along to the called script?


def get_preprocessed_input_csv_file_path(config: dict) -> str:
    """Get the path to the preprocessed input CSV file.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :return: str - The path to the preprocessed input CSV file.
    """
    return (
        os.path.join(config["temp_dir"], os.path.basename(config["input_csv"]))
        + ".preprocessed"
    )


def get_csv_data(
    config: dict, csv_file_target: str = "node_fields", file_path: str = None
) -> DictReader:
    """Read the input CSV data and prepare it for use in all tasks that use an input CSV file.

    This function reads the source CSV file (or the CSV dump from Google Sheets or Excel),
    applies some prepocessing to each CSV record (specifically, it adds any CSV field
    templates that are registered in the config file, and it filters out any CSV
    records or lines in the CSV file that begine with a #), and finally, writes out
    a version of the CSV data to a file that appends .preprocessed to the input
    CSV file name. It is this .preprocessed file that is used in create, update, etc.
    tasks.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        csv_file_target: string
            Either 'node_fields' or 'taxonomy_fields'.
        file_path: string
            The path to the file to check (applies only to vocabulary CSVs).
        Returns
        -------
        preprocessed_csv_reader
            The CSV DictReader object.
    """
    global commented_out_input_csv_rows_present

    if csv_file_target == "node_fields":
        file_path = config["input_csv"]

    if os.path.isabs(file_path):
        input_csv_path = file_path
    elif file_path.startswith("http") is True:
        input_csv_path = get_extracted_csv_file_path(config)
        if os.path.exists(input_csv_path):
            os.remove(input_csv_path)
        get_csv_from_google_sheet(config)
    elif file_path.endswith(".xlsx") is True:
        input_csv_path = get_extracted_csv_file_path(config)
        if os.path.exists(input_csv_path):
            os.remove(input_csv_path)
        get_csv_from_excel(config)
    else:
        input_csv_path = os.path.join(config["input_dir"], file_path)

    if not os.path.exists(input_csv_path):
        message = "CSV file " + input_csv_path + " not found."
        logging.error(message)
        sys.exit("Error: " + message)

    try:
        # 'utf-8-sig' encoding skips Microsoft BOM (0xef, 0xbb, 0xbf) at the start of files,
        # e.g. exported from Excel and has no effect when reading standard UTF-8 encoded files.
        csv_reader_file_handle = open(
            input_csv_path, "r", encoding="utf-8-sig", newline=""
        )
    except UnicodeDecodeError:
        message = (
            "Error: CSV file " + input_csv_path + " must be encoded in ASCII or UTF-8."
        )
        logging.error(message)
        sys.exit(message)

    preprocessed_csv_path = get_preprocessed_input_csv_file_path(config)

    csv_writer_file_handle = open(
        preprocessed_csv_path, "w+", newline="", encoding="utf-8"
    )
    # The CSV reader argument 'restval' is used to populate superfluous fields/labels.
    csv_reader = csv.DictReader(
        csv_reader_file_handle,
        delimiter=config["delimiter"],
        restval="stringtopopulateextrafields",
    )

    # Unfinished (e.g. still need to apply this to creating taxonomies) WIP on #559.
    if config["csv_headers"] == "labels" and config["task"] in [
        "create",
        "update",
        "create_terms",
        "update_terms",
    ]:
        """
        if config['task'] == 'create_terms' or config['task'] == 'update_terms':
            field_map = get_fieldname_map(config, 'taxonomy_term', config['vocab_id'], 'labels')
        else:
            field_map = get_fieldname_map(config, 'node', config['content_type'], 'labels')
        """
        csv_reader_fieldnames = replace_field_labels_with_names(
            config, csv_reader.fieldnames
        )
    else:
        csv_reader_fieldnames = csv_reader.fieldnames

    # Even though we check for the task's expected ID column in the incoming CSV in check_input(),
    # we need to check it here as well since check_input() reads the CSV prior to those checks.
    # TODO: Why can't these checks exist in check_input() only?
    id_columns = {
        "create": config["id_field"],
        "update": "node_id",
        "delete": "node_id",
        "add_media": "node_id",
        "delete_media": "media_id",
        "delete_media_by_node": "node_id",
        "update_media": "media_id",
        "update_media_by_node": "node_id",
        "create_terms": "term_name",
        "update_terms": "term_id",
        "export_csv": "node_id",
    }
    for task, id_column in id_columns.items():
        if (
            task == config["task"]
            and id_columns[config["task"]] not in csv_reader_fieldnames
        ):
            message = f'"{task}" tasks require a "{id_columns[task]}" CSV column. Please check your input CSV file and try again.'
            logging.error(message)
            sys.exit("Error: " + message)

    if config["task"] == "run_scripts":
        run_scripts_entity_type = config["run_scripts_entity_type"]
        id_columns = {"node": "node_id", "media": "media_id", "term": "term_id"}
        if id_columns[config["run_scripts_entity_type"]] not in csv_reader_fieldnames:
            message = f'"run_scripts" tasks for "{run_scripts_entity_type}" entities require a "{id_columns[run_scripts_entity_type]}" CSV column. Please check your input CSV file and try again.'
            logging.error(message)
            sys.exit("Error: " + message)

    if config["task"] == "update_media_by_node":
        csv_reader_fieldnames.append("media_id")

    confirmed = []
    duplicates = []
    for item in csv_reader_fieldnames:
        if item not in confirmed:
            confirmed.append(item)
        else:
            duplicates.append(item)
    if len(duplicates) > 0:
        message = "Error: CSV has duplicate header names - " + ", ".join(duplicates)
        logging.error(message)
        sys.exit(message)
    csv_reader_fieldnames = [
        x for x in csv_reader_fieldnames if x not in config["ignore_csv_columns"]
    ]

    #  If configured to do so, add "field_viewer_override" to output CSV so we can autopopulate the field_viewer_override column.
    if config["task"] == "create" and (
        "field_viewer_override_extensions" in config
        or "field_viewer_override_models" in config
    ):
        if "field_viewer_override" not in csv_reader_fieldnames:
            csv_reader_fieldnames.append("field_viewer_override")

    # CSV field templates and CSV value templates currently apply only to node CSV files, not vocabulary CSV files.
    tasks = ["create", "update", "add_media"]
    if (
        config["task"] in tasks
        and csv_file_target == "node_fields"
        or config["task"] == "run_scripts"
    ):
        # If the config file contains CSV field templates, append them to the CSV data.
        # Make a copy of the column headers so we can skip adding templates to the new CSV
        # if they're present in the source CSV. We don't want fields in the source CSV to be
        # stomped on by templates.
        csv_reader_fieldnames_orig = copy.copy(csv_reader_fieldnames)
        if "csv_field_templates" in config:
            for template in config["csv_field_templates"]:
                for field_name, field_value in template.items():
                    if field_name not in csv_reader_fieldnames_orig:
                        csv_reader_fieldnames.append(field_name)

        csv_writer = csv.DictWriter(
            csv_writer_file_handle,
            fieldnames=csv_reader_fieldnames,
            delimiter=config["delimiter"],
        )
        csv_writer.writeheader()
        row_num = 0
        unique_identifiers = []

        # Prepare any "csv_row_filters", which we apply to each row, below.
        if "csv_row_filters" in config and len(config["csv_row_filters"]) > 0:
            row_filters_is = dict()
            row_filters_isnot = dict()
            # First define the field/operator pairs.
            for filter_config in config["csv_row_filters"]:
                filter_group = filter_config.split(":", 2)
                if filter_group[1] == "is":
                    filter_group_field = filter_group[0]
                    # TODO: delete below unused variable or add it to row_filters_is[filter_group_field]?
                    filter_group_value = filter_group[2]
                    row_filters_is[filter_group_field] = []
                if filter_group[1] == "isnot":
                    filter_group_field = filter_group[0]
                    # TODO: delete below unused variable or add it to row_filters_isnot[filter_group_field]?
                    filter_group_value = filter_group[2]
                    row_filters_isnot[filter_group_field] = []

            # Then populate the lists of filter values.
            for filter_config in config["csv_row_filters"]:
                filter_group = filter_config.split(":", 2)
                # Prepare the '' filter value.
                if filter_group[2] == "''" or filter_group[2] == '""':
                    filter_group[2] = ""
                if filter_group[1] == "is":
                    filter_group_field = filter_group[0]
                    filter_group_value = filter_group[2]
                    row_filters_is[filter_group_field].append(
                        filter_group_value.strip()
                    )
                if filter_group[1] == "isnot":
                    filter_group_field = filter_group[0]
                    filter_group_value = filter_group[2]
                    row_filters_isnot[filter_group_field].append(
                        filter_group_value.strip()
                    )

        if config["task"] == "run_scripts":
            if config["run_scripts_entity_type"] == "node":
                config["id_field"] = "node_id"
            if config["run_scripts_entity_type"] == "media":
                config["id_field"] = "media_id"
            if config["run_scripts_entity_type"] == "term":
                config["id_field"] = "term_id"

        # We subtract 1 from config['csv_start_row'] so user's expectation of the actual
        # start row match up with Python's 0-based counting.
        if config["csv_start_row"] > 0:
            csv_start_row = config["csv_start_row"] - 1
        else:
            csv_start_row = config["csv_start_row"]

        for row in itertools.islice(csv_reader, csv_start_row, config["csv_stop_row"]):
            row_num += 1

            if (
                "csv_start_row_skip" in config
                and "csv_stop_row_skip" in config
                and (
                    int(config["csv_start_row_skip"])
                    <= int(row_num)
                    <= int(config["csv_stop_row_skip"])
                )
            ):
                continue

            if "node_id" in row and value_is_numeric(row["node_id"]) is False:
                row["node_id"] = get_nid_from_url_alias(config, row["node_id"])

            csv_rows_to_process_allowed_tasks = [
                "create",
                "update",
                "add_media",
                "run_scripts",
            ]

            # If the value in config['csv_rows_to_process'] is a path to a file, skip rows not identified in the file.
            if (
                "csv_rows_to_process" in config
                and config["task"] in csv_rows_to_process_allowed_tasks
                and len(config["csv_rows_to_process"]) > 0
                and isinstance(config["csv_rows_to_process"], str)
            ):
                path_to_ids_file = os.path.abspath(config["csv_rows_to_process"])
                if os.path.exists(path_to_ids_file):
                    with open(path_to_ids_file) as fh:
                        ids_to_process = fh.read().splitlines()
                        ids_to_process = [x for x in ids_to_process if x]
                else:
                    message = f'File identified in the "csv_rows_to_process" config setting ({path_to_ids_file}) cannot be found.'
                    logging.error(message)
                    sys.exit("Error: " + message)
                if row[config["id_field"]] not in ids_to_process:
                    continue

            # If the value of in config['csv_rows_to_process'] is a list, skip the rows not in the list.
            if (
                "csv_rows_to_process" in config
                and config["task"] in csv_rows_to_process_allowed_tasks
                and len(config["csv_rows_to_process"]) > 0
                and isinstance(config["csv_rows_to_process"], list)
            ):
                config["csv_rows_to_process"] = [
                    str(x) for x in config["csv_rows_to_process"]
                ]
                if row[config["id_field"]] not in config["csv_rows_to_process"]:
                    continue

            # Apply the "is" and "isnot" csv_row_filters defined above. If the field/value
            # combo is in the 'isnot' list, skip this row.
            filter_out_this_csv_row = False
            if "csv_row_filters" in config and len(config["csv_row_filters"]) > 0:
                if len(row_filters_isnot) > 0:
                    for filter_field, filter_values in row_filters_isnot.items():
                        if len(filter_values) > 0 and filter_field in row:
                            # Split out multiple field values to test each one.
                            values_in_row_field = row[filter_field].split(
                                config["subdelimiter"]
                            )
                            for value_in_row_field in values_in_row_field:
                                filter_out_this_csv_row = False
                                if value_in_row_field.strip() in filter_values:
                                    filter_out_this_csv_row = True
                                else:
                                    break
                if filter_out_this_csv_row is True:
                    continue

                # If the field/value combo is not in the 'is' list, skip this row.
                if len(row_filters_is) > 0:
                    for filter_field, filter_values in row_filters_is.items():
                        if len(filter_values) > 0 and filter_field in row:
                            # Split out multiple field values to test each one.
                            values_in_row_field = row[filter_field].split(
                                config["subdelimiter"]
                            )
                            for value_in_row_field in values_in_row_field:
                                filter_out_this_csv_row = False
                                if value_in_row_field.strip() not in filter_values:
                                    filter_out_this_csv_row = True
                                else:
                                    break
                if filter_out_this_csv_row is True:
                    continue

            # Remove columns specified in config['ignore_csv_columns'].
            if len(config["ignore_csv_columns"]) > 0:
                for column_to_ignore in config["ignore_csv_columns"]:
                    if column_to_ignore in row:
                        del row[column_to_ignore]

            if "csv_field_templates" in config:
                for template in config["csv_field_templates"]:
                    for field_name, field_value in template.items():
                        if field_name not in csv_reader_fieldnames_orig:
                            row[field_name] = field_value

            # Skip CSV records whose first column begins with #.
            if str(list(row.values())[0]).strip().startswith("#") is False:
                # Skip row if the entity is not found.
                if row[config["id_field"]] is False:
                    continue

                if "node_id" in row and value_is_numeric(row["node_id"]) is False:
                    row["node_id"] = get_nid_from_url_alias(config, row["node_id"])

                try:
                    unique_identifiers.append(row[config["id_field"]])

                    if (
                        "csv_value_templates" in config
                        and len(config["csv_value_templates"]) > 0
                    ):
                        row = apply_csv_value_templates(
                            config, "csv_value_templates", row
                        )

                    #  If configured to do so, populate field_viewer_override column.
                    if config["task"] == "create" and (
                        "field_viewer_override_extensions" in config
                        or "field_viewer_override_models" in config
                    ):
                        row["field_viewer_override"] = (
                            get_field_viewer_override_from_condition(config, row)
                        )

                    # Convert node URLs into node IDs.
                    if config["task"] in [
                        "update",
                        "delete",
                        "add_media",
                        "delete_media_by_node",
                        "update_media_by_node",
                    ]:
                        incoming_node_id = copy.copy(row["node_id"])
                        if value_is_numeric(row["node_id"]) is False:
                            row["node_id"] = get_nid_from_url_alias(
                                config, row["node_id"]
                            )
                            if row["node_id"] is False:
                                logging.warning(
                                    f'URL "{incoming_node_id}" not found or is not accessible, skipping update.'
                                )

                    row = clean_csv_values(config, row)
                    csv_writer.writerow(row)
                except ValueError:
                    # Note: this message is also generated in check_input().
                    message = (
                        "Row "
                        + str(row_num)
                        + " (ID "
                        + row[config["id_field"]]
                        + ') of the CSV file "'
                        + input_csv_path
                        + '" '
                        + "has more columns ("
                        + str(len(row))
                        + ") than there are headers ("
                        + str(len(csv_reader.fieldnames))
                        + ")."
                    )
                    logging.error(message)
                    print("Error: " + message)
                    sys.exit(message)
            else:
                commented_out_input_csv_rows_present = True

        repeats = set(
            ([x for x in unique_identifiers if unique_identifiers.count(x) > 1])
        )
        if isinstance(repeats, set) and len(repeats) > 0:
            message = (
                "Duplicate identifiers in column "
                + config["id_field"]
                + " found: "
                + ",".join(repeats)
                + "."
            )
            logging.error(message)
            sys.exit("Error: " + message)
    # "if" applies to create and update tasks for nodes; "else" applies to everything else.
    else:
        csv_writer = csv.DictWriter(
            csv_writer_file_handle,
            fieldnames=csv_reader_fieldnames,
            delimiter=config["delimiter"],
        )
        csv_writer.writeheader()
        row_num = 0
        # We subtract 1 from config['csv_start_row'] so user's expectation of the actual
        # start row match up with Python's 0-based counting.
        if config["csv_start_row"] > 0:
            csv_start_row = config["csv_start_row"] - 1
        else:
            csv_start_row = config["csv_start_row"]
        for row in itertools.islice(csv_reader, csv_start_row, config["csv_stop_row"]):
            row_num += 1

            if (
                "csv_start_row_skip" in config
                and "csv_stop_row_skip" in config
                and (
                    int(config["csv_start_row_skip"])
                    <= int(row_num)
                    <= int(config["csv_stop_row_skip"])
                )
            ):
                continue

            # Remove columns specified in config['ignore_csv_columns'].
            if len(config["ignore_csv_columns"]) > 0:
                for column_to_ignore in config["ignore_csv_columns"]:
                    if column_to_ignore in row:
                        del row[column_to_ignore]

            # Skip CSV records whose first column begins with #.
            if str(list(row.values())[0]).strip().startswith("#") is False:

                if "node_id" in row and value_is_numeric(row["node_id"]) is False:
                    row["node_id"] = get_nid_from_url_alias(config, row["node_id"])

                try:
                    # Get the media ID(s) attached to the node at row["node_id"]. If there is only one
                    # media ID, write it to the .preprocessed CSV file in the "media_id" column. If there
                    # are none, or more than one, log that and move on to next row.
                    if config["task"] == "update_media_by_node":
                        node_media_ids = get_node_media_ids(
                            config,
                            row["node_id"],
                            media_use_tids=config[
                                "update_media_by_node_media_use_tids"
                            ],
                            media_type=config["media_type"],
                        )
                        if len(node_media_ids) == 1:
                            row["media_id"] = node_media_ids[0]
                        elif len(node_media_ids) == 0:
                            logging.warning(
                                f'No matching media on node {row["node_id"]} found.'
                            )
                            continue
                        else:
                            logging.warning(
                                f'Multiple matching media on node {row["node_id"]} found, with media IDs {", ".join([str(x) for x in node_media_ids]).strip()}. Workbench can only update one media per node at a time.'
                            )
                            continue

                    row = clean_csv_values(config, row)
                    csv_writer.writerow(row)
                except ValueError:
                    # Note: this message is also generated in check_input().
                    message = (
                        "Row "
                        + str(row_num)
                        + " (ID "
                        + row[config["id_field"]]
                        + ') of the CSV file "'
                        + input_csv_path
                        + '" '
                        + "has more columns ("
                        + str(len(row))
                        + ") than there are headers ("
                        + str(len(csv_reader.fieldnames))
                        + ")."
                    )
                    logging.error(message)
                    sys.exit("Error: " + message)

    csv_writer_file_handle.close()
    preprocessed_csv_reader_file_handle = open(
        preprocessed_csv_path, "r", encoding="utf-8"
    )
    preprocessed_csv_reader = csv.DictReader(
        preprocessed_csv_reader_file_handle,
        delimiter=config["delimiter"],
        restval="stringtopopulateextrafields",
    )
    return preprocessed_csv_reader


def get_field_vocabularies(
    config: dict, field_definitions: dict, field_name: str
) -> Union[list, bool]:
    """Gets IDs of vocabularies linked from the current field (could be more than one).
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The field definitions.
    :param field_name: string - The field machine name.
    :return: list|bool - The list of vocabulary IDs, or False if there are none.
    """
    if "vocabularies" in field_definitions[field_name]:
        vocabularies = field_definitions[field_name]["vocabularies"]
        return vocabularies
    else:
        return False


def get_csv_id_to_node_id_map_allowed_hosts_sql(config: dict) -> str:
    """Derive a query snippet for SQL SELECT queries from values in the "csv_id_to_node_id_map_allowed_hosts" config
    setting. This snippet is inserted into queries against the CSV ID to node ID map to limit results to rows that
    have the one of the hosts named in "csv_id_to_node_id_map_allowed_hosts" in their "host" column. This snippet is
    intended to be located in the middle of the query, specifically, it ends in "and".
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        Returns
        -------
        The SQL query string, e.g. " host is null or host in ("https://localhost","https://localhost.dev") and "
        Note that this is a literal string, and does not contain any parameterized SQL query placeholders.
        If "csv_id_to_node_id_map_allowed_hosts" is an empty list, this snippet is empty, which in effect
        does not limit the SQL query against the CSV ID to node ID map to specific "host" field values.
    """
    allowed_hosts = copy.copy(config["csv_id_to_node_id_map_allowed_hosts"])
    if len(config["csv_id_to_node_id_map_allowed_hosts"]) > 0:
        # Since the user needs to add the current host to this list, we need to make sure it doesn't
        # contain any trailing / or path information. Assumes the protocol (e.g. https) is the same.
        for i in range(len(allowed_hosts)):
            if allowed_hosts[i].startswith(config["host"]):
                allowed_hosts[i] = config["host"]
        # "" represents an empty host value.
        if "" in config["csv_id_to_node_id_map_allowed_hosts"]:
            allowed_hosts.remove("")
            empty_host_query_string = "host is null or "
        else:
            empty_host_query_string = ""
        hosts_in_parameter = ",".join(f'"{h}"' for h in allowed_hosts)
        sql_snippet = f" {empty_host_query_string} host in ({hosts_in_parameter}) and "
    else:
        sql_snippet = ""

    return sql_snippet


def get_field_viewer_override_from_condition(config: dict, row: OrderedDict) -> str:
    """Derive value for the field_viewer_override CSV column based on conditions defined in configuration.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        row: OrderedDict
            A CSV row. For pages/children created from subdirectories, this
            is a version of the parent's row so we can get $csv_value values for non-required fields.
        Returns
        -------
        The term ID, term name, or the URI for the term from the Islandora Display vocabulary (whatever was
        used in the configuration setting).
    """
    # If the field_viewer_override column is populated, don't change it.
    if "field_viewer_override" in row and len(row["field_viewer_override"].strip()) > 0:
        return row["field_viewer_override"]

    return_value = ""
    # Get the field_viewer_override value from the row's field_model value first.
    if (
        "field_viewer_override_models" in config
        and config["field_viewer_override_models"] is not None
        and len(config["field_viewer_override_models"]) > 0
    ):
        for override in config["field_viewer_override_models"]:
            for islandora_display_term_id, conditions in override.items():
                conditions = [x.lower() for x in conditions]
                if row["field_model"].lower() in conditions:
                    # If multiple matches, last match pertains.
                    return_value = islandora_display_term_id

    # Then get field_viewer_override value from the extension in the row's "file" field, replacing the earlier assigned term ID/name/URI if necessary.
    if (
        "field_viewer_override_extensions" in config
        and config["field_viewer_override_extensions"] is not None
        and len(config["field_viewer_override_extensions"]) > 0
    ):
        if len(row["file"].strip()) == 0:
            return row["field_viewer_override"]

        if row["file"].strip().startswith("http"):
            extension = get_remote_file_extension(config, row["file"])
            extension = extension.lstrip(".").lower()
        else:
            _filename, extension = os.path.splitext(row["file"])
            extension = extension.lstrip(".").lower()

        for override in config["field_viewer_override_extensions"]:
            for islandora_display_term_id, conditions in override.items():
                conditions = [x.lower() for x in conditions]
                conditions = [x.lstrip(".") for x in conditions]
                if extension in conditions:
                    # If multiple matches, last match pertains.
                    return_value = islandora_display_term_id

    return return_value



def check_csv_file_exists(
    config: dict, csv_file_target: str, file_path: str = None
) -> str:
    """Confirms a CSV file exists.Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    csv_file_target: string
        Either 'node_fields' or 'taxonomy_fields'.
    file_path: string
        The path to the file to check (applies only to vocabulary CSVs).
    Returns
    -------
    string
        The absolute file path to the CSV file.
    """
    if csv_file_target == "node_fields":
        if os.path.isabs(config["input_csv"]):
            input_csv = config["input_csv"]
        # For Google Sheets, the "extraction" is fired over in workbench.
        elif config["input_csv"].startswith("http"):
            input_csv = get_extracted_csv_file_path(config)
            message = (
                "Extracting CSV data from "
                + config["input_csv"]
                + " (worksheet gid "
                + str(config["google_sheets_gid"])
                + ") to "
                + input_csv
                + "."
            )
            print(message)
            logging.info(message)
        elif config["input_csv"].endswith("xlsx"):
            input_csv = get_extracted_csv_file_path(config)
            message = (
                "Extracting CSV data from "
                + config["input_csv"]
                + " to "
                + input_csv
                + "."
            )
            print(message)
            logging.info(message)
        else:
            input_csv = os.path.join(config["input_dir"], config["input_csv"])

        if os.path.exists(input_csv):
            message = "OK, CSV file " + input_csv + " found."
            print(message)
            logging.info(message)
            return input_csv
        else:
            message = "CSV file " + input_csv + " not found."
            logging.error(message)
            sys.exit("Error: " + message)
    elif csv_file_target == "taxonomy_fields":
        # For Google Sheets and Excel, the "extraction" is fired in workbench.
        if os.path.isabs(file_path):
            input_csv = file_path
        else:
            input_csv = os.path.join(config["input_dir"], file_path)

        if os.path.exists(input_csv):
            message = "OK, vocabulary CSV file " + input_csv + " found."
            print(message)
            logging.info(message)
            return input_csv
        else:
            message = "Vocabulary CSV file " + input_csv + " not found."
            logging.error(message)
            sys.exit("Error: " + message)


def get_csv_template(config: dict, args: Namespace):
    """Generates a CSV file template for node import based on field definitions.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param args: Namespace - The command-line arguments passed to Workbench.
    """
    field_definitions = get_field_definitions(config, "node")

    field_labels = collections.OrderedDict()
    field_labels["REMOVE THIS COLUMN (KEEP THIS ROW)"] = "LABEL (REMOVE THIS ROW)"
    for field_name in field_definitions:
        if field_definitions[field_name]["label"] != "":
            field_labels[field_name] = field_definitions[field_name]["label"]
        else:
            field_labels[field_name] = ""

    required = collections.OrderedDict()
    required["REMOVE THIS COLUMN (KEEP THIS ROW)"] = (
        "REQUIRED IN CREATE TASKS (REMOVE THIS ROW)"
    )
    for field_name in field_definitions:
        if field_definitions[field_name]["required"] != "":
            if field_definitions[field_name]["required"] is True:
                required[field_name] = "Yes"
            else:
                required[field_name] = "No"
    required["title"] = "Yes"
    required["uid"] = "No"
    required["langcode"] = "No"
    required["created"] = "No"
    required[config["id_field"]] = "Yes"
    if config["nodes_only"] is True:
        required["file"] = "Yes"
    else:
        required["file"] = "No"

    mapping = dict()
    mapping["string"] = "Free text"
    mapping["string_long"] = "Free text"
    mapping["text"] = "Free text"
    mapping["text_long"] = "Free text"
    mapping["geolocation"] = "+49.16,-123.93"
    mapping["entity_reference"] = "100 [or term name or http://foo.com/someuri]"
    mapping["edtf"] = "2020-10-28"
    mapping["typed_relation"] = "relators:art:30"
    mapping["integer"] = 100

    sample_data = collections.OrderedDict()
    sample_data["REMOVE THIS COLUMN (KEEP THIS ROW)"] = "SAMPLE DATA (REMOVE THIS ROW)"
    sample_data[config["id_field"]] = "0001"
    sample_data["file"] = "myimage.jpg"
    sample_data["uid"] = "21"
    sample_data["langcode"] = "fr"
    sample_data["created"] = "2020-11-15T23:49:22+00:00"
    sample_data["title"] = "Free text"

    for field_name in field_definitions:
        if field_definitions[field_name]["field_type"] in mapping:
            sample_data[field_name] = mapping[
                field_definitions[field_name]["field_type"]
            ]
        else:
            sample_data[field_name] = ""

    csv_file_path = os.path.join(
        config["input_dir"], config["input_csv"] + ".csv_file_template"
    )
    csv_file = open(csv_file_path, "a+", encoding="utf-8")
    writer = csv.DictWriter(
        csv_file, fieldnames=sample_data.keys(), lineterminator="\n"
    )
    writer.writeheader()
    # We want the labels and required rows to appear as the second and third rows so
    # add them before we add the sample data.
    writer.writerow(field_labels)
    writer.writerow(required)
    writer.writerow(sample_data)

    cardinality = collections.OrderedDict()
    cardinality["REMOVE THIS COLUMN (KEEP THIS ROW)"] = (
        "NUMBER OF VALUES ALLOWED (REMOVE THIS ROW)"
    )
    cardinality[config["id_field"]] = "1"
    cardinality["file"] = "1"
    cardinality["uid"] = "1"
    cardinality["langcode"] = "1"
    cardinality["created"] = "1"
    cardinality["title"] = "1"
    for field_name in field_definitions:
        if field_definitions[field_name]["cardinality"] == -1:
            cardinality[field_name] = "unlimited"
        else:
            cardinality[field_name] = field_definitions[field_name]["cardinality"]
    writer.writerow(cardinality)

    docs = dict()
    docs["string"] = "Single-valued fields"
    docs["string_long"] = "Single-valued fields"
    docs["text"] = "Single-valued fields"
    docs["text_long"] = "Single-valued fields"
    docs["geolocation"] = "Geolocation fields"
    docs["entity_reference"] = "Taxonomy reference fields"
    docs["edtf"] = "EDTF fields"
    docs["typed_relation"] = "Typed Relation fields"
    docs["integer"] = "Single-valued fields"

    docs_tips = collections.OrderedDict()
    docs_tips["REMOVE THIS COLUMN (KEEP THIS ROW)"] = (
        "SECTION IN DOCUMENTATION (REMOVE THIS ROW)"
    )
    docs_tips[config["id_field"]] = "Required fields"
    docs_tips["file"] = "Required fields"
    docs_tips["uid"] = "Base fields"
    docs_tips["langcode"] = "Base fields"
    docs_tips["created"] = "Base fields"
    docs_tips["title"] = "Base fields"
    for field_name in field_definitions:
        if field_definitions[field_name]["field_type"] in docs:
            doc_reference = docs[field_definitions[field_name]["field_type"]]
            docs_tips[field_name] = doc_reference
        else:
            docs_tips[field_name] = ""
    docs_tips["field_member_of"] = ""
    writer.writerow(docs_tips)

    csv_file.close()
    print("CSV template saved at " + csv_file_path + ".")
    sys.exit()


def get_page_title_from_template(config: dict, parent_title: str, weight: str) -> str:
    """Generates a page title from a simple template.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        parent_title : string
            The parent node's title.
        weight: string
            The weight value for the given page.
        Returns
        -------
        string
            The output of the template.
    """
    page_title_template = string.Template(config["page_title_template"])
    page_title = str(
        page_title_template.substitute({"parent_title": parent_title, "weight": weight})
    )
    return page_title


def csv_subset_warning(config: dict) -> None:
    """Create a message indicating that a subset of the input CSV data will be used,
    specific to the current configuration settings.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    """

    preprocessed_input_csv_file_path = get_preprocessed_input_csv_file_path(config)
    review_message = f'You should review "{preprocessed_input_csv_file_path}" to ensure the correct rows will be processed.'

    if "csv_row_filters" in config and len(config["csv_row_filters"]) > 0:
        message = f'Using a subset of input CSV defined in your "csv_row_filters" setting. {review_message}'
        logging.info(message)
        return None

    if "csv_rows_to_process" in config:
        if len(config["csv_rows_to_process"]) > 0 and isinstance(
            config["csv_rows_to_process"], str
        ):
            path_to_ids_file = os.path.abspath(config["csv_rows_to_process"])
            if os.path.exists(path_to_ids_file):
                with open(path_to_ids_file) as fh:
                    ids_to_process = fh.read().splitlines()
                    ids_to_process = [x for x in ids_to_process if x]
                message = (
                    f'Using a subset of input CSV rows listed in "{path_to_ids_file}".'
                )
                print(message)
                logging.info(message)
                return None
            else:
                message = f'File identified in the "csv_rows_to_process" config setting ({path_to_ids_file}) cannot be found.'
                logging.error(message)
                sys.exit("Error: " + message)

        if len(config["csv_rows_to_process"]) > 0 and isinstance(
            config["csv_rows_to_process"], list
        ):
            message = f'Using a subset of input CSV defined in your "csv_rows_to_process" setting. {review_message}'
            print(message)
            logging.info(message)
            return None

    if config["csv_start_row"] != 0 or config["csv_stop_row"] is not None:
        csv_reader = CsvReader(config)
        csv_data = list(csv_reader.get_csv_data())
        start_row_id = csv_data[0][config["id_field"]]
        stop_row_id = csv_data[-1][config["id_field"]]

        message = f"Using a subset of the input CSV (will start at row {config['csv_start_row']} / row ID \"{start_row_id}\", stop at row {config['csv_stop_row']} / row ID \"{stop_row_id}\")."
        if config["csv_start_row"] != 0 and config["csv_stop_row"] is None:
            message = f"Using a subset of the input CSV (will start at row {config['csv_start_row']} / row ID {start_row_id})."
        if config["csv_start_row"] == 0 and config["csv_stop_row"] is not None:
            message = f"Using a subset of the input CSV (will stop at row {config['csv_stop_row']} / row ID {stop_row_id})."
        print(message)
        logging.info(message)

    if "csv_start_row_skip" in config and "csv_stop_row_skip" in config:
        message = f"Using a subset of the input CSV (will skip rows {config['csv_start_row_skip']} to {config['csv_stop_row_skip']})."
        print(message)
        logging.info(message)

    return None


def get_entity_reference_view_endpoints(config: dict) -> dict:
    """Gets entity reference View endpoints from config.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        Returns
        -------
        dict
            Dictionary with Drupal field names as keys and View REST endpoints as values.
    """
    endpoint_mappings = dict()
    if "entity_reference_view_endpoints" not in config:
        return endpoint_mappings

    for endpoint_mapping in config["entity_reference_view_endpoints"]:
        for field_name, endpoint in endpoint_mapping.items():
            endpoint_mappings[field_name] = endpoint

    return endpoint_mappings


def get_node_exists_verification_view_endpoint(config: dict) -> Union[tuple, bool]:
    """Gets from config the View endpoints and CSV field to match to determine if a matching node already exists.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        Returns
        -------
        tuple|False
            Tuple containing Drupal field name and View REST endpoints as values. If there are multiple mappings,
            the returned tuple will contain the field_name and endpoint values of only the last mapping. If the config
            can't be loaded into a tuple, returns False.
    """
    for endpoint_mapping in config["node_exists_verification_view_endpoint"]:
        for field_name, endpoint in endpoint_mapping.items():
            endpoint_mapping = (field_name, endpoint)

    if type(endpoint_mapping) is tuple:
        return endpoint_mapping
    else:
        return False


def get_percentage(part: Union[int, str, float], whole: Union[int, str, float]):
    """Calculates percentage of 'part' out of 'whole'.
    Parameters
    :param part: int|str|float - The part value.
    :param whole: int|str|float - The whole value.
    :return: float - The percentage value.
    """
    return 100 * float(part) / float(whole)


def log_json(source_data):
    """If config setting log_json is true, logs the JSON used in a HTTP request.

    Params
    ----------
        source_data : dict
            The data to be used in the request body.
    Return
    ------
        None
    """
    try:
        json_to_log = json.dumps(source_data)
        logging.info(json_to_log)
    except Exception as e:
        logging.warning(e)
        logging.warning(
            f"Can't convert data to JSON, logging it instead. More info may be available in the previous log message. Data is: {source_data}"
        )


def paged_content_ignore_file(config: dict, filename_to_check: str) -> bool:
    """Checks to see if a given page filename matches an entry in the
    paged_content_ignore_files config setting.
    Params
    ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        filename_to_check : string
            The filename to check. Is lower-cased and stripped prior to the check.
    Return
    ------
        bool
            True if the file is to be ignored, False if the file is not be ignored.
    """
    # Normalize the incoming filename and paged_content_ignore_files entries.
    filename_to_check_normalized = filename_to_check.lower().strip()
    filters = copy.copy(config["paged_content_ignore_files"])
    for i in range(len(filters)):
        filters[i] = filters[i].lower().strip()

    # * wildcard is the filename (e.g., ["*.db", "*.xml"]), so we can match on the extension with the wildcard filename.
    # First, get a list of the *. patterns from configuration.
    filename_is_wildcard_patterns = [fnp for fnp in filters if fnp.startswith("*.")]
    # Then see if the filename_to_check ends with any of the extensions in those patterns.
    extension_matches = any(
        # Get rid of the leading * but keep the period.
        filename_to_check_normalized.endswith(filename.lstrip("*"))
        for filename in filename_is_wildcard_patterns
    )
    if extension_matches is True:
        return True

    # * wildcard is the extension (e.g., ["matchme.*", "imamatch.*"]), so we can match on the filename with the wildcard extension.
    # First, get a list of the .* patterns from configuration.
    extension_is_wildcard_patterns = [extp for extp in filters if extp.endswith(".*")]
    # Then see if the filename_to_check starts with any of the filenames in those patterns.
    filename_matches = any(
        # Get rid of the trailing * but keep the period.
        filename_to_check_normalized.startswith(filename.rstrip("*"))
        for filename in extension_is_wildcard_patterns
    )
    if filename_matches is True:
        return True

    # Whole filenames with extensions.
    if filename_to_check.strip().lower() in [
        fn.strip().lower() for fn in config["paged_content_ignore_files"]
    ]:
        return True

    return False


def is_running_in_docker() -> bool:
    """Assumes Dockerfile sets an environment variable ISLANDORA_WORKBENCH_IS_RUNNING_IN_DOCKER.
    If that environment variable is not present, return False.
    """
    if "ISLANDORA_WORKBENCH_IS_RUNNING_IN_DOCKER" in os.environ:
        return True
    else:
        return False


def patch_revision_log_message(entity_type: str, revision_log_message: str) -> dict:
    """Return the JSON object for a PATCH request required to patch the entity's revision log message.

    Parameters:
        :param entity_type: str - The entity type (e.g., 'node', 'media').
        :param revision_log_message: str - The revision log message.

    Returns:
        The JSON request for a PATCH request required to patch the entity's revision log message.
    """
    if "entity_type" not in ["node", "media"]:
        logging.error(
            f"Invalid entity type ({entity_type}) passed to patch_media_revision_log_message function."
        )
    revision_log_message_key = (
        "revision_log" if entity_type == "node" else "revision_log_message"
    )
    return {revision_log_message_key: [{"value": revision_log_message}]}
