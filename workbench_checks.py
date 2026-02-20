import copy
import datetime
import json
import logging
import os
import subprocess
import time
from argparse import Namespace
from pathlib import Path

import requests

import workbench_utils
import sys

from workbench_exceptions import WorkbenchValidationException
from workbench_utils import ping_islandora, check_integration_module_version, \
    WorkbenchCsvReader, get_entity_fields, check_rollback_file_path_directories, check_for_required_config_keys, \
    create_temp_dir, validate_input_dir, check_csv_file_exists, replace_field_labels_with_names, ping_remote_file, \
    get_field_definitions, validate_geolocation_fields, validate_link_fields, validate_authority_link_fields, \
    validate_edtf_fields, validate_csv_field_cardinality, validate_csv_field_length, validate_text_list_fields, \
    validate_taxonomy_field_values, validate_typed_relation_field_values, validate_numeric_fields, \
    validate_media_track_fields, ping_node, get_entity_reference_view_endpoints, ping_view_endpoint, \
    validate_language_code, set_media_type, ping_media_bundle, get_remote_file_extension, \
    get_registered_media_extensions, get_additional_files_config, validate_media_use_tid_in_additional_files_setting, \
    check_file_exists, paged_content_ignore_file, get_sequence_indicator_from_filename, validate_weight_value, \
    file_is_utf8, ping_media, ping_term, get_file_hash_from_local, generate_contact_sheet_from_csv


def check(self):
    pass

def _simple_config_checks(config: dict):
    # Check the config file.
    tasks = ["create", "update", "delete", "add_media", "update_media", "update_media_by_node", "delete_media",
        "delete_media_by_node", "create_from_files", "create_terms", "export_csv", "get_data_from_view",
        "get_media_report_from_view", "update_terms", "create_redirects", "add_alt_text", "update_alt_text",
        "run_scripts", ]

    if config["task"] not in tasks:
        message = (
                f'"task" in your configuration file must be one of {", ".join(tasks)}, but it is currently set to "{config["task"]}".')
        logging.error(message)
        sys.exit("Error: " + message)

    if config["csv_id_to_node_id_map_dir"] == config["temp_dir"]:
        message = f'You should set your "csv_id_to_node_id_map_dir" config setting to a location other than your system\'s temporary directory ("{config["temp_dir"]}").'
        logging.warning(message)

    if workbench_utils.is_running_in_docker():
        docker_message = (
                "It appears you are running Workbench within a Docker container. Please ensure that your CSV ID to node ID map " + "config setting defines a location accessible outside of the Docker container; otherwise, it might be deleted when you destroy or rebuild your Docker image.")
        print("Warning: " + docker_message)
        logging.warning(docker_message)

    if (
            config["recovery_mode_starting_from_node_id"] and
            workbench_utils.value_is_numeric(config["recovery_mode_starting_from_node_id"])
    ):
        message = f'"recovery_mode" option in effect. Items that have already been ingested with node IDs starting at {config["recovery_mode_starting_from_node_id"]} will be skipped.'
        print(message)
        logging.info(message)

def _check_host_values(config: dict, field_names: list):
    # Check to see if there are any "host" column values in the CSV ID to node ID map that
    # aren't empty or the current config["host"] value.
    # This is the set of conditions where the map is queried to get parent node IDs. AFAIK it's
    # complete but if others come up, they should be added here.
    if (len(config["csv_id_to_node_id_map_allowed_hosts"]) > 0 or (
            os.environ.get("ISLANDORA_WORKBENCH_SECONDARY_TASKS") is not None) or (
            "parent_id" in field_names and config[
        "query_csv_id_to_node_id_map_for_parents"] is True) or (
            config["recovery_mode_starting_from_node_id"] is not False and workbench_utils.value_is_numeric(
        config["recovery_mode_starting_from_node_id"]
        ) is True)):
        csv_to_node_id_map_path = config["csv_id_to_node_id_map_path"]
        current_host = config["host"]

        workbench_utils.prepare_csv_id_to_node_id_map(config)
        check_for_host_column_result = workbench_utils.sqlite_manager(
            config, operation="select", db_file_path=csv_to_node_id_map_path,
            query="select * from pragma_table_info(?)", values=("csv_id_to_node_id_map",), )
        if check_for_host_column_result[-1][1] == "host":
            num_unique_hosts_result = workbench_utils.sqlite_manager(
                config, operation="select", db_file_path=csv_to_node_id_map_path,
                query="select distinct host from csv_id_to_node_id_map", )

            unique_host_values = [ x[0].strip() for x in num_unique_hosts_result if x[0] is not None and x[0].strip() != "" and x[0].strip() != current_host ]

            list_of_hosts = ", ".join(unique_host_values).strip()
            if len(unique_host_values) > 0:
                multiple_hosts_in_map_log_message = (
                        'There are values for the "host" column in the CSV ID to node ID map ' + f'at "{csv_to_node_id_map_path}" other than "" (empty) and your currently configured host ("{current_host}"). ' + f"Those extra hosts are {list_of_hosts}. Please see https://mjordan.github.io/islandora_workbench_docs/csv_id_to_node_id_map/" + " for advice on what to do.")
                logging.warning(multiple_hosts_in_map_log_message)
                multiple_hosts_in_map_console_message = (
                        'There are values for the "host" column in the CSV ID to node ID map ' + f'at "{csv_to_node_id_map_path}" other than your current "host" configuration setting. Please see your workbench log for more information.')
                print("Warning: " + multiple_hosts_in_map_console_message)
            else:
                logging.info(
                    'No unexpected values in the CSV ID to node ID map\'s "host" column.'
                )

def _check_get_data_from_view(config: dict, config_filename: str):
    # Perform checks on get_data_from_view tasks. Since this task doesn't use input_dir, input_csv, etc.,
    # we exit immediately after doing these checks.
    # First, ping the View.
    view_parameters = ("&".join(config["view_parameters"]) if "view_parameters" in config else "")
    view_url = (config["host"] + "/" + config["view_path"].lstrip("/") + "?page=0&" + view_parameters)

    view_path_status_code = workbench_utils.ping_view_endpoint(config, view_url)
    view_url_for_message = config["host"] + "/" + config["view_path"].lstrip("/")
    if view_path_status_code != 200:
        message = f"Cannot access View at {view_url_for_message}."
        logging.error(message)
        sys.exit("Error: " + message)
    else:
        message = f'View REST export at "{view_url_for_message}" is accessible.'
        logging.info(message)
        print("OK, " + message)

    if config["export_file_directory"] is not None:
        if not os.path.exists(config["export_file_directory"]):
            try:
                os.mkdir(config["export_file_directory"])
                os.rmdir(config["export_file_directory"])
            except Exception as e:
                message = ('Path in configuration option "export_file_directory" ("' + config[
                    "export_file_directory"] + '") is not writable.')
                logging.error(message + " " + str(e))
                sys.exit("Error: " + message + " See log for more detail.")

    if config["export_file_media_use_term_id"] is False:
        message = f'Unknown value for configuration setting "export_file_media_use_term_id": {config["export_file_media_use_term_id"]}.'
        logging.error(message)
        sys.exit("Error: " + message)

    # Check to make sure the output path for the CSV file is writable.
    if config["export_csv_file_path"] is not None:
        csv_file_path = config["export_csv_file_path"]
    else:
        csv_file_path = os.path.join(
            config["input_dir"], os.path.splitext(os.path.basename(config_filename))[0] + ".csv_file_with_data_from_view", )
    with open(csv_file_path, "a") as csv_file_path_file:
        if not csv_file_path_file.writable():
            message = f'Path to CSV file "{csv_file_path}" is not writable.'
            logging.error(message)
            sys.exit("Error: " + message)
        else:
            message = f"CSV output file location at {csv_file_path} is writable."
            logging.info(message)
            print("OK, " + message)

    if os.path.exists(csv_file_path):
        os.remove(csv_file_path)

    # If nothing has failed by now, exit with a positive, upbeat message.
    if config["perform_soft_checks"] is True:
        always_review_log_message = ""
    else:
        always_review_log_message = (" However, you should review your Workbench log after running --check.")
    config_and_data_appear_to_be_valid_message = f"Configuration and input data appear to be valid.{always_review_log_message}"
    print(config_and_data_appear_to_be_valid_message)
    if config["perform_soft_checks"] is True:
        print(
            'Warning: "perform_soft_checks" is enabled so you need to review your log for errors despite the "OK" reports above.'
        )

    logging.info(
        'Configuration checked for "%s" task using config file "%s", no problems found.', config["task"],
        config_filename)

def _check_media_csv_headers(config: dict, csv_headers: list):
    field_definitions = workbench_utils.get_field_definitions(config, "media", config["media_type"])
    base_media_fields = ["status", "uid", "langcode"]
    drupal_fieldnames = list(field_definitions.keys())
    base_columns = ["media_id", "file", "node_id"]
    additional = list(workbench_utils.get_additional_files_config(config).keys())

    for csv_column_header in csv_headers:
        if (
                csv_column_header not in drupal_fieldnames and
                csv_column_header not in base_columns and
                csv_column_header not in base_media_fields and
                csv_column_header not in additional
        ):
            logging.error(
                'CSV column header "%s" does not match any Drupal field names in the "%s" media type',
                csv_column_header, config["media_type"], )
            sys.exit(
                'Error: CSV column header "' + csv_column_header + '" does not match any Drupal field names in the "' +
                config["media_type"] + '" media type.'
            )
    message = "OK, CSV column headers match Drupal field names."
    print(message)
    logging.info(message)

def task_specific_checks(config: dict, reader: workbench_utils.WorkbenchCsvReader, reserved_fields: list):

    node_base_fields = ["title", "status", "promote", "sticky", "uid", "created", "published", ]
    csv_column_headers = copy.copy(reader.get_field_names())
    additional_files = workbench_utils.get_additional_files_config(config)

    if config["task"] == "create":
        _task_specific_checks_create(config, reader, reserved_fields, node_base_fields, csv_column_headers, additional_files)
    elif config["task"] == "update":
        _task_specific_checks_update(config, reader, node_base_fields, csv_column_headers)
    elif config["task"] == "update_media":
        _task_specific_checks_update_media(config, reader)
    elif config["task"] == "update_media_by_node":
        _task_specific_checks_update_media_by_node(config, reader)
    elif config["task"] == "add_media":
        _task_specific_checks_add_media(config, reader)
    elif config["task"] == "create_terms":
        _task_specific_checks_create_terms(config, reader)
    elif config["task"] == "update_terms":
        _task_specific_checks_update_terms(config, reader, csv_column_headers)
    elif config["task"] in ["add_alt_text", "update_alt_text"]:
        _task_specific_checks_add_or_update_alt_text(config, reader)

def _task_specific_checks_create(config: dict, reader: workbench_utils.WorkbenchCsvReader, reserved_fields: list, node_base_fields: list, csv_column_headers: list, additional_files: dict):
    """Perform checks specific to "create" tasks. Note that some checks are the same as for "update tasks, but there are also some differences, so we have a separate function for "create" task checks.
    Parameters
    :param config: dict - the configuration dictionary
    :param reader: WorkbenchCsvReader - the CSV reader object
    :param reserved_fields: list - the list of reserved fields
    :param node_base_fields: list - the list of base fields for nodes
    :param csv_column_headers: list - the list of column headers in the CSV file
    :param additional_files: dict - the "additional_files" configuration setting, which is used to check for additional file field names in the CSV column headers
    """
    additional_files_keys = list(additional_files.keys())

    field_definitions = workbench_utils.get_field_definitions(config, "node")
    if config["id_field"] not in csv_column_headers:
        message = 'For "create" tasks, your CSV file must have a column containing a unique identifier.'
        logging.error(message)
        sys.exit("Error: " + message)
    if (config["nodes_only"] is False and "file" not in csv_column_headers and (
            config["paged_content_from_directories"] is False or config[
        "paged_content_from_directories_parents_exist"] is False)):
        message = 'For "create" tasks, your CSV file must contain a "file" column.'
        logging.error(message)
        sys.exit("Error: " + message)
    if "title" not in csv_column_headers:
        message = 'For "create" tasks, your CSV file must contain a "title" column.'
        logging.error(message)
        sys.exit("Error: " + message)
    if "output_csv" in config.keys():
        if os.path.exists(config["output_csv"]):
            message = ("Output CSV already exists at " + config["output_csv"] + ", records will be appended to it.")
            print(message)
            logging.info(message)

    # We populate the ISLANDORA_WORKBENCH_PRIMARY_TASK_EXECUTION_START_TIME environment variable here so secondary
    # tasks can access it during in validate_parent_ids_in_csv_id_to_node_id_map().
    workbench_execution_start_time = "{:%Y-%m-%d %H:%M:%S}".format(
        datetime.datetime.now()
    )
    # Assumes that only primary tasks have something in their 'secondary_tasks' config setting.
    if config["secondary_tasks"] is not None:
        os.environ["ISLANDORA_WORKBENCH_PRIMARY_TASK_EXECUTION_START_TIME"] = (workbench_execution_start_time)
    if "parent_id" in csv_column_headers:
        # TODO: Move into CSV preprocessing to avoid having to read the CSV file twice.
        workbench_utils.validate_parent_ids_precede_children(
            config, reader
        )
        workbench_utils.prepare_csv_id_to_node_id_map(config)
        if config["query_csv_id_to_node_id_map_for_parents"] is True:
            validate_parent_ids_in_csv_id_to_node_id_map_csv_data = (reader.get_csv_data())
            workbench_utils.validate_parent_ids_in_csv_id_to_node_id_map(
                config, validate_parent_ids_in_csv_id_to_node_id_map_csv_data
            )
        else:
            message = f"Only node IDs for parents created during this session will be used (not using the CSV ID to node ID map)."
            print(message)
            logging.warning(message)

    # Specific to creating aggregated content such as collections, compound objects and paged content. Currently, if 'parent_id' is present
    # in the CSV file 'field_member_of' is mandatory.
    if "parent_id" in csv_column_headers:
        if "field_weight" not in csv_column_headers:
            message = 'If you are ingesting compound objects, a "field_weight" column is required in your input CSV file.'
            logging.info(message)
        if "field_member_of" not in csv_column_headers:
            message = 'If your CSV file contains a "parent_id" column, it must also contain a "field_member_of" column (with empty values in child rows).'
            logging.error(message)
            sys.exit("Error: " + message)
    drupal_fieldnames = list(field_definitions.keys())

    if len(drupal_fieldnames) == 0:
        message = "Workbench cannot retrieve field definitions from Drupal. Please confirm that the Field, Field Storage, and Entity Form Display REST resources are enabled."
        logging.error(message)
        sys.exit("Error: " + message)

    if config["list_missing_drupal_fields"] is True:

        missing_drupal_fields = []
        for csv_column_header in csv_column_headers:
            if csv_column_header not in drupal_fieldnames and csv_column_header not in node_base_fields:
                if csv_column_header not in reserved_fields and csv_column_header not in additional_files_keys:
                    if csv_column_header != config["id_field"]:
                        missing_drupal_fields.append(csv_column_header)
        if len(missing_drupal_fields) > 0:
            missing_drupal_fields_message = ", ".join(missing_drupal_fields)
            logging.error(
                "The following header(s) require a matching Drupal field name: %s.", missing_drupal_fields_message, )
            sys.exit(
                "Error: The following header(s) require a matching Drupal field name: " + missing_drupal_fields_message + "."
            )

    # We .remove() CSV column headers for this check because they are not Drupal field names (including 'langcode').
    for reserved_field in reserved_fields:
        if reserved_field in csv_column_headers:
            csv_column_headers.remove(reserved_field)

    # langcode is a standard Drupal field but it doesn't show up in any field configs.
    if "langcode" in csv_column_headers:
        csv_column_headers.remove("langcode")

    # We .remove() CSV column headers that use the 'media:video:field_foo' media track convention.
    media_track_headers = list()
    for column_header in csv_column_headers:
        if column_header.startswith("media:"):
            media_track_header_parts = column_header.split(":")
            if (media_track_header_parts[1] in config["media_track_file_fields"].keys() and
                    media_track_header_parts[2] == config["media_track_file_fields"][
                        media_track_header_parts[1]]):
                media_track_headers.append(column_header)
    for media_track_header in media_track_headers:
        if media_track_header in csv_column_headers:
            csv_column_headers.remove(media_track_header)

    # We also validate the structure of the media track column headers.
    for media_track_header in media_track_headers:
        media_track_header_parts = media_track_header.split(":")
        if (media_track_header_parts[0] != "media" and len(media_track_header_parts) != 3):
            message = (f'"{media_track_header}" is not a valide media track CSV header.')
            logging.error(message)
            sys.exit("Error: " + message)

    # Check the configuration that is necessary for verifying nodes already exist in the target Drupal.
    if "node_exists_verification_view_endpoint" in config:
        node_exists_config = workbench_utils.get_node_exists_verification_view_endpoint(config)
        if node_exists_config is not False:
            if node_exists_config[0] not in csv_column_headers:
                message = f'CSV column identified in "node_exists_verification_view_endpoint" is not in your CSV file.'
                logging.error(message)
                sys.exit("Error: " + message)
            view_url = f'{config["host"]}/{node_exists_config[1].lstrip("/")}'
            view_path_status_code = workbench_utils.ping_view_endpoint(config, view_url)
            if view_path_status_code != 200:
                message = f'Cannot access View REST export configured in "node_exists_verification_view_endpoint" ({view_url}).'
                logging.error(message)
                sys.exit("Error: " + message)
            else:
                message = f'View REST export configured in "node_exists_verification_view_endpoint" ({view_url}) is accessible. Values in the "{node_exists_config[0]}" CSV column will be used to check whether nodes already exist.'
                logging.info(message)
                print("OK, " + message)

    # Check for the View that is necessary for entity reference fields configured
    # as "Views: Filter by an entity reference View" (issue 452).
    for csv_column_header in csv_column_headers:
        if (csv_column_header in field_definitions and field_definitions[csv_column_header][
            "handler"] == "views"):
            if (config[
                "require_entity_reference_views"] is True and "entity_reference_view_endpoints" not in config):
                entity_reference_view_exists = workbench_utils.ping_entity_reference_view_endpoint(
                    config, csv_column_header, field_definitions[csv_column_header]["handler_settings"], )
                if entity_reference_view_exists is False:
                    console_message = ('Workbench cannot access the View "' +
                                       field_definitions[csv_column_header]["handler_settings"]["view"][
                                           "view_name"] + '" required to validate values for field "' + csv_column_header + '". See log for more detail.')
                    log_message = ('Workbench cannot access the path defined by the REST Export display "' +
                                   field_definitions[csv_column_header]["handler_settings"]["view"][
                                       "display_name"] + '" in the View "' +
                                   field_definitions[csv_column_header]["handler_settings"]["view"][
                                       "view_name"] + '" required to validate values for field "' + csv_column_header + '". Please check your Drupal Views configuration.' + ' See the "Entity Reference Views fields" section of ' + "https://mjordan.github.io/islandora_workbench_docs/fields/#entity-reference-views-fields for more info.")
                    logging.error(log_message)
                    sys.exit("Error: " + console_message)
            else:
                message = f'Workbench will not validate values in your CSV file\'s "{csv_column_header}" column because your "require_entity_reference_views" configuration setting is "false".'
                print("Warning: " + message)
                logging.warning(
                    message + ' See the "Entity Reference Views fields" section of ' + "https://mjordan.github.io/islandora_workbench_docs/fields/#entity-reference-views-fields for more info."
                )

        if len(additional_files) > 0:
            if csv_column_header not in drupal_fieldnames and csv_column_header not in node_base_fields and csv_column_header not in additional_files:
                if csv_column_header in config["ignore_csv_columns"] or csv_column_header in additional_files_keys:
                    continue
                logging.error(
                    'CSV column header %s does not match any Drupal, reserved, or "additional_files" field names.',
                    csv_column_header, )
                sys.exit(
                    'Error: CSV column header "' + csv_column_header + '" does not match any Drupal, reserved, or "additional_files" field names.'
                )
        else:
            if (
                    csv_column_header not in drupal_fieldnames and csv_column_header not in node_base_fields and csv_column_header):
                if csv_column_header in config["ignore_csv_columns"]:
                    continue
                logging.error(
                    "CSV column header %s does not match any Drupal or reserved field names.",
                    csv_column_header, )
                sys.exit(
                    'Error: CSV column header "' + csv_column_header + '" does not match any Drupal or reserved field names.'
                )
    message = "OK, CSV column headers match Drupal field names."
    print(message)
    logging.info(message)

    if ("field_viewer_override_extensions" in config or "field_viewer_override_models" in config):
        preprocessed_input_csv_file_path = reader.get_preprocessed_csv_filepath()
        message = f'You should review "{preprocessed_input_csv_file_path}" to ensure that values in the "field_viewer_override" column have been correctly assigned based on your configuration settings.'
        print("Warning: " + message)
        logging.warning(message)

    # Check that Drupal fields that are required are in the 'create' task CSV file.
    required_drupal_fields_node = workbench_utils.get_required_bundle_fields(
        config, "node", config["content_type"]
    )
    for required_drupal_field in required_drupal_fields_node:
        if required_drupal_field not in csv_column_headers:
            logging.error(
                "Required Drupal field %s is not present in the CSV file.", required_drupal_field, )
            sys.exit(
                'Error: Field "' + required_drupal_field + '" required for content type "' + config[
                    "content_type"] + '" is not present in the CSV file.'
            )
    message = "OK, required Drupal fields are present in the CSV file."
    print(message)
    logging.info(message)

    if not config["nodes_only"]:
        workbench_utils.validate_media_use_tid(config)

    if config["fixity_algorithm"] is not None:
        allowed_algorithms = ["md5", "sha1", "sha256"]
        if config["fixity_algorithm"] not in allowed_algorithms:
            message = ("Configured fixity algorithm '" + config[
                "fixity_algorithm"] + "' must be one of 'md5', 'sha1', or 'sha256'.")
            logging.error(message)
            sys.exit("Error: " + message)

    if config["validate_fixity_during_check"] and config["fixity_algorithm"] is not None:
        field_and_checksum_in_csv = False
        fixity_message = "Performing local checksum validation as part of checks."
        logging.info(fixity_message)
        print(fixity_message + " This might take some time.")
        checksum_validation_all_ok = True

    # Generate a list of geolocation fields
    geolocation_fields = [key for key, val in field_definitions.items() if val["field_type"] == "geolocation"]
    # Generate a list of geolocation fields that are present in the CSV file.
    geolocation_fields_present = [field for field in geolocation_fields if field in reader.get_field_names()]
    # Set this to True if we find any geolocation fields in the CSV file, so we can report at the end of the checks whether geolocation field validation was performed.
    is_geolocation_fields_present = False

    # Generate a list of link fields
    link_fields = [key for key, val in field_definitions.items() if val["field_type"] == "link"]
    # Generate a list of link fields that are present in the CSV file.
    link_fields_present = [field for field in link_fields if field in reader.get_field_names()]
    # Set this to True if we find any link fields in the CSV file, so we can report at the end of the checks whether link field validation was performed.
    is_link_fields_present = False

    # Generate a list of authority link fields
    authority_link_fields = [key for key, val in field_definitions.items() if val["field_type"] == "authority_link"]
    # Generate a list of authority link fields that are present in the CSV file.
    authority_link_fields_present = [field for field in authority_link_fields if field in reader.get_field_names()]
    # Set this to True if we find any authority link fields in the CSV file, so we can report at the end of the checks whether authority link field validation was performed.
    is_authority_link_fields_present = False

    # Generate a list of EDTF fields
    edtf_fields = [key for key, val in field_definitions.items() if val["field_type"] == "edtf"]
    # Generate a list of EDTF fields that are present in the CSV file.
    edtf_fields_present = [field for field in edtf_fields if field in reader.get_field_names()]
    # Set this to True if we find any EDTF fields in the CSV file, so we can report at the end of the checks whether EDTF field validation was performed.
    is_edtf_fields_present = False

    # Generate a list of fields with cardinality > 1 that are present in the CSV file.
    cardinality_fields_present = {key: field_definitions[key]["cardinality"] for key in csv_column_headers if
                                  field_definitions[key]["cardinality"] > 0 and key != "title"}

    # Generate a list of taxonomy term reference fields
    taxonomy_reference_fields = [key for key, val in field_definitions.items() if
                                 val["field_type"] == "entity_reference" and val["target_type"] == "taxonomy_term"]
    # Generate a list of taxonomy term reference fields that are present in the CSV file.
    taxonomy_reference_fields_present = [field for field in taxonomy_reference_fields if
                                         field in reader.get_field_names()]
    # Set this to True if we find any taxonomy term reference fields in the CSV file, so we can report at the end of the checks whether taxonomy term reference field validation was performed.
    is_taxonomy_fields_present = False
    taxonomy_reference_errors = []
    for taxonomy_reference_field in taxonomy_reference_fields_present:
        try:
            vocabularies = workbench_utils.get_field_vocabularies(
                config, field_definitions, taxonomy_reference_field
            )
            _ = len(vocabularies)
        except TypeError:
            message = (
                f'Workbench cannot get vocabularies linked to field "{taxonomy_reference_field}". Please confirm that field has at least one vocabulary.')
            taxonomy_reference_errors.append(message)
    if len(taxonomy_reference_errors) > 0:
        error_messages = "\n".join(taxonomy_reference_errors)
        logging.error(f"Errors found in taxonomy reference field configuration:\n{error_messages}")
        sys.exit(
            f"Error: Errors found in taxonomy reference field configuration. See log for more detail.\n{error_messages}"
        )

    # Generate a list of typed relation reference fields
    typed_relation_reference_fields = [key for key, val in field_definitions.items() if
                                       val["field_type"] == "typed_relation" and "typed_relations" in val[key]]
    # Generate a list of typed relation reference fields that are present in the CSV file.
    typed_relation_reference_fields_present = [field for field in typed_relation_reference_fields if
                                               field in reader.get_field_names()]
    is_typed_relation_reference_fields_present = False

    # Try to do all the checks that require CSV data in a single loop through the file, to avoid having to read it multiple times. We have to use the reader.get_csv_data() method to get the data for these checks instead of the csv_reader.get_csv_data() method because we need the preprocessing that happens in reader.get_csv_data() for some of these checks (e.g. validate_url_aliases()).
    # TODO: Move all these checks into CSV preprocessing to avoid having to iterate over every row of the CSV file multiple times.
    rows_with_errors = []
    for row_num, row in enumerate(reader.get_csv_data(), start=1):
        try:
            workbench_utils.validate_required_fields_have_values_action(config, required_drupal_fields_node, row)
        except WorkbenchValidationException as e:
            rows_with_errors.append((row_num, str(e)))

        try:
            if "created" in csv_column_headers:
                # Validate dates in 'created' field, if present.
                workbench_utils.validate_node_created_date_action(config, row)
        except WorkbenchValidationException as e:
            rows_with_errors.append((row_num, str(e)))

        try:
            if "uid" in csv_column_headers:
                workbench_utils.validate_node_uid_action(config, row)
        except WorkbenchValidationException as e:
            rows_with_errors.append((row_num, str(e)))

        try:
            if "url_alias" in csv_column_headers:
                workbench_utils.validate_url_aliases_action(config, row)
        except WorkbenchValidationException as e:
            rows_with_errors.append((row_num, str(e)))

        if config["nodes_only"] is False and "media_use_tid" in row:
            delimited_field_values = row["media_use_tid"].split(config["subdelimiter"])
            for field_value in delimited_field_values:
                if len(field_value.strip()) > 0:
                    try:
                        workbench_utils.validate_media_use_tid(config, field_value, row[config["id_field"]])
                    except WorkbenchValidationException as e:
                        rows_with_errors.append((row_num, str(e)))

        if config["validate_fixity_during_check"] and config["fixity_algorithm"] is not None:
            if "file" in row and "checksum" in row:
                file_name = row["file"]
                field_and_checksum_in_csv = True
                if not file_name.lower().startswith("http"):
                    filepath = Path(file_name)
                    if not filepath.is_absolute():
                        filepath = Path(config["input_dir"]) / filepath
                    hash_from_local = workbench_utils.get_file_hash_from_local(
                        config, str(filepath), config["fixity_algorithm"]
                    )
                    if hash_from_local is not False and hash_from_local == row["checksum"].strip():
                        logging.info(
                            'Local %s checksum and value in the CSV "checksum" field for file "%s" (%s) match.',
                            config["fixity_algorithm"], filepath, hash_from_local, )
                    elif hash_from_local is not False:
                        checksum_validation_all_ok = False
                        logging.warning(
                            'Local %s checksum and value in the CSV "checksum" field for file "%s" (named in CSV row "%s") do not match (local: %s, CSV: %s).',
                            config["fixity_algorithm"], filepath, row[config["id_field"]], hash_from_local,
                            row["checksum"], )

        if "image_alt_text" in row:
            if len(row["image_alt_text"]) > config["max_image_alt_text_length"]:
                image_alt_text = row["image_alt_text"]
                max_alt_text_length = config["max_image_alt_text_length"]
                node_id = row[config["id_field"]]
                message = f"Alt text in input CSV row with node ID {node_id} is longer than the maximum configured alt text length ({max_alt_text_length})"
                logging.warning(
                    message
                    + f" (length is {len(image_alt_text)} characters). Adding the alt text in this row will be skipped."
                )
                print("Warning: " + message + ". See log for more information.")

        if len(geolocation_fields_present) > 0:
            for geolocation_field in geolocation_fields_present:
                if geolocation_field in row and len(row[geolocation_field]) > 0:
                    is_geolocation_fields_present = True
                    try:
                        workbench_utils.validate_geolocation_fields_action(config, row, geolocation_field)
                    except WorkbenchValidationException as e:
                        rows_with_errors.append((row_num, str(e)))

        if len(link_fields_present) > 0:
            for link_field in link_fields_present:
                if link_field in row and len(row[link_field]) > 0:
                    link_value = row[link_field].strip()
                    is_link_fields_present = True
                    delimited_field_values = link_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0 and not workbench_utils.validate_link_value(field_value):
                            message = (
                                f'Value in field "{link_field}" in row with ID {row[config["id_field"]]} (' +
                                f'{field_value}) is not a valid link field value.'
                            )
                            rows_with_errors.append((row_num, message))

        if len(authority_link_fields_present) > 0:
            for authority_link_field in authority_link_fields_present:
                if authority_link_field in row and len(row[authority_link_field]) > 0:
                    is_authority_link_fields_present = True
                    authority_link_value = row[authority_link_field].strip()
                    delimited_field_values = authority_link_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if (len(field_value.strip()) > 0 and
                                not workbench_utils.validate_authority_link_value(
                                    field_value.strip(),
                                    field_definitions[authority_link_field]["authority_sources"],
                        )):
                            message = (
                                f'Value in field "{authority_link_field}" in row with ID {row[config["id_field"]]} (' +
                                f'{field_value}) is not a valid authority link field value.'
                            )
                            rows_with_errors.append((row_num, message))

        if len(edtf_fields_present) > 0:
            for edtf_field in edtf_fields_present:
                if edtf_field in row and len(row[edtf_field]) > 0:
                    is_edtf_fields_present = True
                    edtf_value = row[edtf_field].strip()
                    delimited_field_values = edtf_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0 and not workbench_utils.validate_edtf_date(field_value.strip()):
                            message = (
                                f'Value in field "{edtf_field}" in row with ID {row["node_id"]} (' +
                                f'{field_value}) is not a valid EDTF date/time.'
                            )
                            rows_with_errors.append((row_num, message))

        if len(cardinality_fields_present) > 0:
            for cardinality_field, cardinality_value in cardinality_fields_present:
                delimited_field_values = row[cardinality_field].split(config["subdelimiter"])
                if len(delimited_field_values) > cardinality_value:
                    message = (f'CSV field "{cardinality_field}" in record with ID {row["node_id"]} contains more values ' +
                    f'than the number of values allowed for that field (max {cardinality_value}). Workbench with add only the first{"" if cardinality_value == 1 else f" {cardinality_value}"} value(s) for that field.')
                    logging.warning(message)
                    print(f"Warning: {message}")

        if len(taxonomy_reference_fields_present) > 0:
            new_terms: bool = False
            for taxonomy_reference_field in taxonomy_reference_fields_present:
                if len(row[taxonomy_reference_field]) > 0:
                    is_taxonomy_fields_present = True
                    new_terms = workbench_utils.validate_taxonomy_reference_value_action(config, field_definitions, taxonomy_reference_field, row[taxonomy_reference_field], row[config["id_field"]])
            if new_terms and config["allow_adding_terms"]:
                if config["validate_terms_exist"]:
                    message = "OK, term IDs/names in CSV file exist in their respective taxonomies (new terms will be created"
                    if config["log_term_creation"] is True:
                        message += " as noted in the Workbench log)."
                    else:
                        message += ' but not noted in the Workbench log since "log_term_creation" is set to false).'
                    print(message)
                else:
                    message = "Skipping check for existence of terms (note: new terms will be created "
                    if config["log_term_creation"] is True:
                        message += "as noted in the Workbench log)."
                    else:
                        message += 'but not noted in the Workbench log - "log_term_creation" is set to false).'
                    print(message)
                    logging.warning(
                        "Skipping check for existence of terms (but new terms will be created)."
                    )
            elif not new_terms:
                # All term IDs are in their field's vocabularies.
                print("OK, term IDs/names in CSV file exist in their respective taxonomies.")
                logging.info(
                    "OK, term IDs/names in CSV file exist in their respective taxonomies."
                )

        if len(typed_relation_reference_fields_present) > 0:
            for typed_relation_reference_field in typed_relation_reference_fields_present:
                if len(row[typed_relation_reference_field]) > 0:
                    delimited_field_values = row[typed_relation_reference_field].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0:
                            try:
                                workbench_utils.validate_typed_relation_field_value_action(
                                    config, field_definitions, typed_relation_reference_field, field_value.strip(), row[config["id_field"]]
                                )
                            except WorkbenchValidationException as e:
                                message = (
                                    f'Value in field "{typed_relation_reference_field}" in row with ID {row[config["id_field"]]} (' +
                                    f'{field_value}) is not a valid typed relation reference field value: {str(e)}'
                                )
                                rows_with_errors.append((row_num, message))

    if len(rows_with_errors) > 0:
        error_messages = "\n".join([f"Row {row_num}: {error_message}" for row_num, error_message in rows_with_errors])
        logging.error(f"Errors found in CSV data:\n{error_messages}")
        sys.exit(f"Error: Errors found in CSV data. See log for more detail.\n{error_messages}")
    else:
        if "url_alias" in csv_column_headers:
            message = "OK, URL aliases do not already exist."
            print(message)
            logging.info(message)
        if "created" in csv_column_headers:
            message = 'OK, dates in the "created" CSV field are all formated correctly and in the future.'
            print(message)
            logging.info(message)
        if "uid" in csv_column_headers:
            message = 'OK, user IDs in the "uid" CSV field all exist.'
            print(message)
            logging.info(message)
        if config["validate_fixity_during_check"] and config["fixity_algorithm"] is not None:
            if field_and_checksum_in_csv:
                if checksum_validation_all_ok:
                    checksum_validation_message = "OK, checksum validation during complete. All checks pass."
                    logging.info(checksum_validation_message)
                    print(checksum_validation_message + " See the log for more detail.")
                else:
                    checksum_validation_message = "Not all checksum validation passed."
                    logging.warning(checksum_validation_message)
                    print(
                        "Warning: " + checksum_validation_message + " See the log for more detail."
                    )
            else:
                checksum_validation_message = 'Could not validate checksums because the input CSV did not contain both "field" and "checksum" columns.'
                print("Warning: " + checksum_validation_message)
                logging.warning(checksum_validation_message)
        if is_geolocation_fields_present:
            message = "OK, geolocation field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_link_fields_present:
            message = "OK, link field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_authority_link_fields_present:
            message = "OK, authority link field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_edtf_fields_present:
            message = "OK, EDTF field values in the CSV file validate."
            print(message)
            logging.info(message)
### END IF TASK == CREATE

def _task_specific_checks_update(config: dict, reader: workbench_utils.WorkbenchCsvReader, node_base_fields: list, csv_column_headers: list):
    """Checks specific to "update" tasks. Note that some checks are the same as for "create tasks, but there are also some differences, so we have a separate function for "update" task checks.
    Parameters
    :param config: dict - the configuration dictionary
    :param reader: WorkbenchCsvReader - the CSV reader object
    :param node_base_fields: list - the list of base node fields
    :param csv_column_headers: list - the list of CSV column headers
    """
    if "node_id" not in csv_column_headers:
        message = ('For "update" tasks, your CSV file must contain a "node_id" column.')
        logging.error(message)
        sys.exit("Error: " + message)

    field_def_start = time.time()
    field_definitions = workbench_utils.get_field_definitions(config, "node")
    field_def_end = time.time()
    print(f"Time to get field definitions: {field_def_end - field_def_start} seconds")
    drupal_fieldnames = []
    for drupal_fieldname in field_definitions:
        drupal_fieldnames.append(drupal_fieldname)
    if "title" in csv_column_headers:
        csv_column_headers.remove("title")
    if "url_alias" in csv_column_headers:
        csv_column_headers.remove("url_alias")
    if "image_alt_text" in csv_column_headers:
        csv_column_headers.remove("image_alt_text")
    if "media_use_tid" in csv_column_headers:
        csv_column_headers.remove("media_use_tid")
    if "revision_log" in csv_column_headers:
        csv_column_headers.remove("revision_log")
    if "file" in csv_column_headers:
        message = 'Error: CSV column header "file" is not allowed in update tasks.'
        logging.error(message)
        sys.exit(message)
    if "node_id" in csv_column_headers:
        csv_column_headers.remove("node_id")

    # langcode is a standard Drupal field but it doesn't show up in any field configs.
    if "langcode" in csv_column_headers:
        csv_column_headers.remove("langcode")

    print("start geolocation fields")
    # Generate a list of geolocation fields
    geolocation_fields = [key for key, val in field_definitions.items() if val["field_type"] == "geolocation"]
    # Generate a list of geolocation fields that are present in the CSV file.
    geolocation_fields_present = [field for field in geolocation_fields if field in reader.get_field_names()]
    # Set this to True if we find any geolocation fields in the CSV file, so we can report at the end of the checks whether geolocation field validation was performed.
    is_geolocation_fields_present = False

    # Generate a list of link fields
    link_fields = [key for key, val in field_definitions.items() if val["field_type"] == "link"]
    # Generate a list of link fields that are present in the CSV file.
    link_fields_present = [field for field in link_fields if field in reader.get_field_names()]
    # Set this to True if we find any link fields in the CSV file, so we can report at the end of the checks whether link field validation was performed.
    is_link_fields_present = False

    # Generate a list of authority link fields
    authority_link_fields = [key for key, val in field_definitions.items() if val["field_type"] == "authority_link"]
    # Generate a list of authority link fields that are present in the CSV file.
    authority_link_fields_present = [field for field in authority_link_fields if field in reader.get_field_names()]
    # Set this to True if we find any authority link fields in the CSV file, so we can report at the end of the checks whether authority link field validation was performed.
    is_authority_link_fields_present = False

    # Generate a list of EDTF fields
    edtf_fields = [key for key, val in field_definitions.items() if val["field_type"] == "edtf"]
    # Generate a list of EDTF fields that are present in the CSV file.
    edtf_fields_present = [field for field in edtf_fields if field in reader.get_field_names()]
    # Set this to True if we find any EDTF fields in the CSV file, so we can report at the end of the checks whether EDTF field validation was performed.
    is_edtf_fields_present = False

    # Generate a list of fields with cardinality > 1 that are present in the CSV file.
    cardinality_fields_present = {key: field_definitions[key]["cardinality"] for key in csv_column_headers if
                                  field_definitions[key]["cardinality"] > 0 and key != "title"}

    print("start taxonomy_reference_fields")
    # Generate a list of taxonomy term reference fields
    taxonomy_reference_fields = [key for key, val in field_definitions.items() if
                                 val["field_type"] == "entity_reference" and val["target_type"] == "taxonomy_term"]
    # Generate a list of taxonomy term reference fields that are present in the CSV file.
    taxonomy_reference_fields_present = [field for field in taxonomy_reference_fields if
                                         field in reader.get_field_names()]
    # Set this to True if we find any taxonomy term reference fields in the CSV file, so we can report at the end of the checks whether taxonomy term reference field validation was performed.
    is_taxonomy_fields_present = False
    taxonomy_reference_errors = []
    for taxonomy_reference_field in taxonomy_reference_fields_present:
        try:
            vocabularies = workbench_utils.get_field_vocabularies(
                config, field_definitions, taxonomy_reference_field
            )
            _ = len(vocabularies)
        except TypeError:
            message = (
                f'Workbench cannot get vocabularies linked to field "{taxonomy_reference_field}". Please confirm that field has at least one vocabulary.')
            taxonomy_reference_errors.append(message)
    if len(taxonomy_reference_errors) > 0:
        error_messages = "\n".join(taxonomy_reference_errors)
        logging.error(f"Errors found in taxonomy reference field configuration:\n{error_messages}")
        sys.exit(
            f"Error: Errors found in taxonomy reference field configuration. See log for more detail.\n{error_messages}"
            )

    print("start typed_relation_reference_fields")
    # Generate a list of typed relation reference fields
    typed_relation_reference_fields = [key for key, val in field_definitions.items() if
                                       val["field_type"] == "typed_relation" and "typed_relations" in val]
    # Generate a list of typed relation reference fields that are present in the CSV file.
    typed_relation_reference_fields_present = [field for field in typed_relation_reference_fields if
                                               field in reader.get_field_names()]
    is_typed_relation_reference_fields_present = False


    for csv_column_header in csv_column_headers:
        if (csv_column_header not in drupal_fieldnames and csv_column_header not in node_base_fields):
            if csv_column_header in config["ignore_csv_columns"]:
                continue
            logging.error(
                "CSV column header %s does not match any Drupal field names in the %s content type.", csv_column_header,
                config["content_type"], )
            sys.exit(
                'Error: CSV column header "' + csv_column_header + '" does not match any Drupal field names in the ' +
                config["content_type"] + " content type."
            )
    message = "OK, CSV column headers match Drupal field names."
    print(message)
    logging.info(message)

    row_errors = []

    for row_num, row in enumerate(reader.get_csv_data(), start=1):
        try:
            if "url_alias" in csv_column_headers:
                workbench_utils.validate_url_aliases_action(config, row)
        except WorkbenchValidationException as e:
            row_errors.append((row_num, str(e)))
            
        if len(geolocation_fields_present) > 0:
            for geolocation_field in geolocation_fields_present:
                if geolocation_field in row and len(row[geolocation_field]) > 0:
                    is_geolocation_fields_present = True
                    try:
                        workbench_utils.validate_geolocation_fields_action(config, row, geolocation_field)
                    except WorkbenchValidationException as e:
                        row_errors.append((row_num, str(e)))

        if len(link_fields_present) > 0:
            for link_field in link_fields_present:
                if link_field in row and len(row[link_field]) > 0:
                    link_value = row[link_field].strip()
                    is_link_fields_present = True
                    delimited_field_values = link_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0 and not workbench_utils.validate_link_value(field_value):
                            message = (
                                f'Value in field "{link_field}" in row with ID {row[config["id_field"]]} (' +
                                f'{field_value}) is not a valid link field value.'
                            )
                            row_errors.append((row_num, message))

        if len(authority_link_fields_present) > 0:
            for authority_link_field in authority_link_fields_present:
                if authority_link_field in row and len(row[authority_link_field]) > 0:
                    is_authority_link_fields_present = True
                    authority_link_value = row[authority_link_field].strip()
                    delimited_field_values = authority_link_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if (len(field_value.strip()) > 0 and
                                not workbench_utils.validate_authority_link_value(
                                    field_value.strip(),
                                    field_definitions[authority_link_field]["authority_sources"],
                        )):
                            message = (
                                f'Value in field "{authority_link_field}" in row with ID {row[config["id_field"]]} (' +
                                f'{field_value}) is not a valid authority link field value.'
                            )
                            row_errors.append((row_num, message))

        if len(edtf_fields_present) > 0:
            for edtf_field in edtf_fields_present:
                if edtf_field in row and len(row[edtf_field]) > 0:
                    is_edtf_fields_present = True
                    edtf_value = row[edtf_field].strip()
                    delimited_field_values = edtf_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0 and not workbench_utils.validate_edtf_date(field_value.strip()):
                            message = (
                                f'Value in field "{edtf_field}" in row with ID {row["node_id"]} (' +
                                f'{field_value}) is not a valid EDTF date/time.'
                            )
                            row_errors.append((row_num, message))

        if len(cardinality_fields_present) > 0:
            for cardinality_field, cardinality_value in cardinality_fields_present:
                delimited_field_values = row[cardinality_field].split(config["subdelimiter"])
                if len(delimited_field_values) > cardinality_value:
                    message = (f'CSV field "{cardinality_field}" in record with ID {row["node_id"]} contains more values ' +
                    f'than the number of values allowed for that field (max {cardinality_value}). Workbench with add only the first{"" if cardinality_value == 1 else f" {cardinality_value}"} value(s) for that field.')
                    logging.warning(message)
                    print(f"Warning: {message}")

        if len(taxonomy_reference_fields_present) > 0:
            new_terms: bool = False
            for taxonomy_reference_field in taxonomy_reference_fields_present:
                if len(row[taxonomy_reference_field]) > 0:
                    is_taxonomy_fields_present = True
                    new_terms = workbench_utils.validate_taxonomy_reference_value_action(config, field_definitions, taxonomy_reference_field, row[taxonomy_reference_field], row[config["id_field"]])
            if new_terms and config["allow_adding_terms"]:
                if config["validate_terms_exist"]:
                    message = "OK, term IDs/names in CSV file exist in their respective taxonomies (new terms will be created"
                    if config["log_term_creation"] is True:
                        message += " as noted in the Workbench log)."
                    else:
                        message += ' but not noted in the Workbench log since "log_term_creation" is set to false).'
                    print(message)
                else:
                    message = "Skipping check for existence of terms (note: new terms will be created "
                    if config["log_term_creation"] is True:
                        message += "as noted in the Workbench log)."
                    else:
                        message += 'but not noted in the Workbench log - "log_term_creation" is set to false).'
                    print(message)
                    logging.warning(
                        "Skipping check for existence of terms (but new terms will be created)."
                    )
            elif not new_terms:
                # All term IDs are in their field's vocabularies.
                print("OK, term IDs/names in CSV file exist in their respective taxonomies.")
                logging.info(
                    "OK, term IDs/names in CSV file exist in their respective taxonomies."
                )

        if len(typed_relation_reference_fields_present) > 0:
            for typed_relation_reference_field in typed_relation_reference_fields_present:
                if len(row[typed_relation_reference_field]) > 0:
                    delimited_field_values = row[typed_relation_reference_field].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0:
                            try:
                                workbench_utils.validate_typed_relation_field_value_action(
                                    config, field_definitions, typed_relation_reference_field, field_value.strip(), row[config["id_field"]]
                                )
                            except WorkbenchValidationException as e:
                                message = (
                                    f'Value in field "{typed_relation_reference_field}" in row with ID {row[config["id_field"]]} (' +
                                    f'{field_value}) is not a valid typed relation reference field value: {str(e)}'
                                )
                                row_errors.append((row_num, message))

    if len(row_errors) > 0:
        error_messages = "\n".join(
            [f"Row {row_num}: {error_message}" for row_num, error_message in row_errors]
            )
        logging.error(f"Errors found in CSV data:\n{error_messages}")
        sys.exit(f"Error: Errors found in CSV data. See log for more detail.\n{error_messages}")
    else:
        if "url_alias" in csv_column_headers:
            message = "OK, URL aliases do not already exist."
            print(message)
            logging.info(message)
        if is_geolocation_fields_present:
            message = "OK, geolocation field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_link_fields_present:
            message = "OK, link field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_authority_link_fields_present:
            message = "OK, authority link field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_edtf_fields_present:
            message = "OK, EDTF field values in the CSV file validate."
            print(message)
            logging.info(message)

def _task_specific_checks_update_media(config: dict, reader: workbench_utils.WorkbenchCsvReader):
    """Checks specific to "update_media" tasks. Note that some checks are the same as for "create_media tasks, but there are also some differences, so we have a separate function for "update_media" task checks.
    Parameters
    :param config: dict - the configuration dictionary
    :param reader: WorkbenchCsvReader - the CSV reader object
    """
    rows_with_errors = []
    for row_num, row in enumerate(reader.get_csv_data(), start=1):
        media_id = workbench_utils.extract_media_id(config, row)
        if media_id is None:
            error_message = "Could not extract media ID from row."
            rows_with_errors.append((row_num, error_message))

    if len(rows_with_errors) > 0:
        error_messages = "\n".join([f"Row {row_num}: {error_message}" for row_num, error_message in rows_with_errors])
        logging.error(f"Errors found in CSV data:\n{error_messages}")
        sys.exit(f"Error: Errors found in CSV data. See log for more detail.\n{error_messages}")

def _task_specific_checks_update_media_by_node(config: dict, reader: workbench_utils.WorkbenchCsvReader):
    """Checks specific to "update_media_by_node" tasks. Note that some checks are the same as for "create_media tasks, but there are also some differences, so we have a separate function for "update_media_by_node" task checks.
    Parameters
    :param config: dict - the configuration dictionary
    :param reader: WorkbenchCsvReader - the CSV reader object
    """
    for row_num, row in enumerate(reader.get_csv_data(), start=1):
        node_media_ids = workbench_utils.get_node_media_ids(
            config, row["node_id"], media_use_tids=config["update_media_by_node_media_use_tids"],
            media_type=config["media_type"], )
        if len(node_media_ids) == 1:
            logging.info(
                f'Matching media on node {{row["node_id"]}} (media ID {node_media_ids[0]}) will be updated.'
            )
        elif len(node_media_ids) == 0:
            logging.warning(f'No matching media on node {row["node_id"]} found.')
        else:
            logging.warning(
                f'Multiple matching media on node {row["node_id"]} found, with media IDs {", ".join([str(x) for x in node_media_ids]).strip()}. Workbench can only update one media per node at a time.'
            )

def _task_specific_checks_add_media(config: dict, reader: workbench_utils.WorkbenchCsvReader):
    """Checks specific to "add_media" tasks. Note that some checks are the same as for "create_media tasks, but there are also some differences, so we have a separate function for add_media" task checks.
    Parameters
    :param config: dict - the configuration dictionary
    :param reader: WorkbenchCsvReader - the CSV reader object
    """
    # These checks are also duplicatd in "_task_specific_checks_create"
    if config["fixity_algorithm"] is not None:
        allowed_algorithms = ["md5", "sha1", "sha256"]
        if config["fixity_algorithm"] not in allowed_algorithms:
            message = ("Configured fixity algorithm '" + config[
                "fixity_algorithm"] + "' must be one of 'md5', 'sha1', or 'sha256'.")
            logging.error(message)
            sys.exit("Error: " + message)

    if config["validate_fixity_during_check"] and config["fixity_algorithm"] is not None:
        field_and_checksum_in_csv = False
        fixity_message = "Performing local checksum validation."
        logging.info(fixity_message)
        print(fixity_message + " This might take some time.")
        validate_checksums_csv_data = reader.get_csv_data()
        row_id = "node_id"
        checksum_validation_all_ok = True
        for checksum_validation_row_count, checksum_validation_row in enumerate(
                validate_checksums_csv_data, start=1
        ):
            file_name = checksum_validation_row["file"]
            if "file" in checksum_validation_row and "checksum" in checksum_validation_row:
                field_and_checksum_in_csv = True
                if not file_name.lower().startswith("http"):
                    filepath = Path(file_name)
                    if not filepath.is_absolute():
                        filepath = Path(config["input_dir"]) / filepath
                    hash_from_local = workbench_utils.get_file_hash_from_local(
                        config, str(filepath), config["fixity_algorithm"]
                    )
                    if hash_from_local is False:
                        continue
                    if "checksum" in checksum_validation_row:
                        if hash_from_local == checksum_validation_row["checksum"].strip():
                            logging.info(
                                'Local %s checksum and value in the CSV "checksum" field for file "%s" (%s) match.',
                                config["fixity_algorithm"], filepath, hash_from_local, )
                        else:
                            checksum_validation_all_ok = False
                            logging.warning(
                                'Local %s checksum and value in the CSV "checksum" field for file "%s" (named in CSV row "%s") do not match (local: %s, CSV: %s).',
                                config["fixity_algorithm"], filepath, checksum_validation_row[row_id], hash_from_local,
                                checksum_validation_row["checksum"], )

        if field_and_checksum_in_csv:
            if checksum_validation_all_ok:
                checksum_validation_message = "OK, checksum validation during complete. All checks pass."
                logging.info(checksum_validation_message)
                print(checksum_validation_message + " See the log for more detail.")
            else:
                checksum_validation_message = "Not all checksum validation passed."
                logging.warning(checksum_validation_message)
                print(
                    "Warning: " + checksum_validation_message + " See the log for more detail."
                )
        else:
            checksum_validation_message = 'Could not validate checksums because the input CSV did not contain both "field" and "checksum" columns.'
            print("Warning: " + checksum_validation_message)
            logging.warning(checksum_validation_message)

def _task_specific_checks_create_terms(config: dict, reader: workbench_utils.WorkbenchCsvReader, csv_headers: list):
    """Checks specific to "create_terms" tasks.
    Parameters
    :param config: dict - the configuration dictionary
    :param reader: WorkbenchCsvReader - the CSV reader object
    :param csv_headers: list - the list of CSV column headers
    """
    # Check that all required fields are present in the CSV.
    field_definitions = workbench_utils.get_field_definitions(
        config, "taxonomy_term", config["vocab_id"]
    )

    # Check here that all required fields are present in the CSV.
    required_fields = workbench_utils.get_required_bundle_fields(
        config, "taxonomy_term", config["vocab_id"]
    )
    required_fields.insert(0, "term_name")
    missing_fields = [ x for x in required_fields if x not in reader.get_field_names() ]
    if len(missing_fields) > 0:
        message = ("Required columns missing from input CSV file: " + ", ".join(missing_fields) + ".")
        logging.error(message)
        sys.exit("Error: " + message)

    # Generate a list of geolocation fields
    geolocation_fields = [ key for key, val in field_definitions.items() if val["field_type"] == "geolocation" ]
    # Generate a list of geolocation fields that are present in the CSV file.
    geolocation_fields_present = [ field for field in geolocation_fields if field in reader.get_field_names() ]
    # Set this to True if we find any geolocation fields in the CSV file, so we can report at the end of the checks whether geolocation field validation was performed.
    is_geolocation_fields_present = False

    # Generate a list of link fields
    link_fields = [ key for key, val in field_definitions.items() if val["field_type"] == "link" ]
    # Generate a list of link fields that are present in the CSV file.
    link_fields_present = [ field for field in link_fields if field in reader.get_field_names() ]
    # Set this to True if we find any link fields in the CSV file, so we can report at the end of the checks whether link field validation was performed.
    is_link_fields_present = False

    # Generate a list of authority link fields
    authority_link_fields = [ key for key, val in field_definitions.items() if val["field_type"] == "authority_link" ]
    # Generate a list of authority link fields that are present in the CSV file.
    authority_link_fields_present = [ field for field in authority_link_fields if field in reader.get_field_names() ]
    # Set this to True if we find any authority link fields in the CSV file, so we can report at the end of the checks whether authority link field validation was performed.
    is_authority_link_fields_present = False

    # Generate a list of EDTF fields
    edtf_fields = [ key for key, val in field_definitions.items() if val["field_type"] == "edtf" ]
    # Generate a list of EDTF fields that are present in the CSV file.
    edtf_fields_present = [ field for field in edtf_fields if field in reader.get_field_names() ]
    # Set this to True if we find any EDTF fields in the CSV file, so we can report at the end of the checks whether EDTF field validation was performed.
    is_edtf_fields_present = False

    # Generate a list of fields with cardinality > 1 that are present in the CSV file.
    cardinality_fields_present = { key: field_definitions[key]["cardinality"] for key in csv_headers if field_definitions[key]["cardinality"] > 0 and key != "title" }

    # Generate a list of taxonomy term reference fields
    taxonomy_reference_fields = [ key for key, val in field_definitions.items() if val["field_type"] == "entity_reference" and val["target_type"] == "taxonomy_term" ]
    # Generate a list of taxonomy term reference fields that are present in the CSV file.
    taxonomy_reference_fields_present = [ field for field in taxonomy_reference_fields if field in reader.get_field_names() ]
    # Set this to True if we find any taxonomy term reference fields in the CSV file, so we can report at the end of the checks whether taxonomy term reference field validation was performed.
    is_taxonomy_fields_present = False
    taxonomy_reference_errors = []
    for taxonomy_reference_field in taxonomy_reference_fields_present:
        try:
            vocabularies = workbench_utils.get_field_vocabularies(
                config, field_definitions, taxonomy_reference_field
            )
            _ = len(vocabularies)
        except TypeError:
            message = (
                    f'Workbench cannot get vocabularies linked to field "{taxonomy_reference_field}". Please confirm that field has at least one vocabulary.')
            taxonomy_reference_errors.append(message)
    if len(taxonomy_reference_errors) > 0:
        error_messages = "\n".join(taxonomy_reference_errors)
        logging.error(f"Errors found in taxonomy reference field configuration:\n{error_messages}")
        sys.exit(f"Error: Errors found in taxonomy reference field configuration. See log for more detail.\n{error_messages}")

    # Generate a list of typed relation reference fields
    typed_relation_reference_fields = [ key for key, val in field_definitions.items() if val["field_type"] == "typed_relation" and "typed_relations" in val[key] ]
    # Generate a list of typed relation reference fields that are present in the CSV file.
    typed_relation_reference_fields_present = [ field for field in typed_relation_reference_fields if field in reader.get_field_names() ]
    is_typed_relation_reference_fields_present = False

    # Validate length of 'term_name'.
    validate_term_name_csv_data = reader.get_csv_data()
    row_errors = []

    for count, row in enumerate(validate_term_name_csv_data, start=1):
        if "term_name" in row and len(row["term_name"]) > 255:
            message = ("The 'term_name' column in row for term '" + row[
                "term_name"] + "' of your CSV file exceeds Drupal's maximum length of 255 characters.")
            row_errors.append((count, message))

        if len(geolocation_fields_present) > 0:
            for geolocation_field in geolocation_fields_present:
                if geolocation_field in row and len(row[geolocation_field]) > 0:
                    is_geolocation_fields_present = True
                    try:
                        workbench_utils.validate_geolocation_fields_action(config, row, geolocation_field)
                    except WorkbenchValidationException as e:
                        row_errors.append((count, str(e)))

        if len(link_fields_present) > 0:
            for link_field in link_fields_present:
                if link_field in row and len(row[link_field]) > 0:
                    link_value = row[link_field].strip()
                    is_link_fields_present = True
                    delimited_field_values = link_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0 and not workbench_utils.validate_link_value(field_value):
                            message = (
                                f'Value in field "{link_field}" in row with ID {row[config["id_field"]]} (' +
                                f'{field_value}) is not a valid link field value.'
                            )
                            row_errors.append((count, message))

        if len(authority_link_fields_present) > 0:
            for authority_link_field in authority_link_fields_present:
                if authority_link_field in row and len(row[authority_link_field]) > 0:
                    is_authority_link_fields_present = True
                    authority_link_value = row[authority_link_field].strip()
                    delimited_field_values = authority_link_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if (len(field_value.strip()) > 0 and
                                not workbench_utils.validate_authority_link_value(
                                    field_value.strip(),
                                    field_definitions[authority_link_field]["authority_sources"],
                        )):
                            message = (
                                f'Value in field "{authority_link_field}" in row with ID {row[config["id_field"]]} (' +
                                f'{field_value}) is not a valid authority link field value.'
                            )
                            row_errors.append((count, message))

        if len(edtf_fields_present) > 0:
            for edtf_field in edtf_fields_present:
                if edtf_field in row and len(row[edtf_field]) > 0:
                    is_edtf_fields_present = True
                    edtf_value = row[edtf_field].strip()
                    delimited_field_values = edtf_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0 and not workbench_utils.validate_edtf_date(field_value.strip()):
                            message = (
                                f'Value in field "{edtf_field}" in row with ID {row["node_id"]} (' +
                                f'{field_value}) is not a valid EDTF date/time.'
                            )
                            row_errors.append((count, message))

        if len(cardinality_fields_present) > 0:
            for cardinality_field, cardinality_value in cardinality_fields_present:
                delimited_field_values = row[cardinality_field].split(config["subdelimiter"])
                if len(delimited_field_values) > cardinality_value:
                    message = (f'CSV field "{cardinality_field}" in record with ID {row["node_id"]} contains more values ' +
                    f'than the number of values allowed for that field (max {cardinality_value}). Workbench with add only the first{"" if cardinality_value == 1 else f" {cardinality_value}"} value(s) for that field.')
                    logging.warning(message)
                    print(f"Warning: {message}")

        for header in csv_headers:
            if "max_length" in field_definitions[header] and field_definitions[header]["max_length"] is not None:
                delimited_field_values = row[header].split(config["subdelimiter"])
                for field_value in delimited_field_values:
                    if len(field_value) > int(field_definitions[header]["max_length"]):
                        message = (f'CSV field "{header}" in record with ID {row["node_id"]} contains a value that is ' +
                                   f'longer ({str(len(field_value))} characters) than allowed for that field ({str(field_definitions[header]["max_length"])}) ' +
                                    ' characters). Workbench will truncate this value prior to populating Drupal.')
                        logging.warning(message)
                        print(f"Warning: {message}")

        if len(taxonomy_reference_fields_present) > 0:
            new_terms: bool = False
            for taxonomy_reference_field in taxonomy_reference_fields_present:
                if len(row[taxonomy_reference_field]) > 0:
                    is_taxonomy_fields_present = True
                    new_terms = workbench_utils.validate_taxonomy_reference_value_action(config, field_definitions, taxonomy_reference_field, row[taxonomy_reference_field], row[config["id_field"]])
            if new_terms and config["allow_adding_terms"]:
                if config["validate_terms_exist"]:
                    message = "OK, term IDs/names in CSV file exist in their respective taxonomies (new terms will be created"
                    if config["log_term_creation"] is True:
                        message += " as noted in the Workbench log)."
                    else:
                        message += ' but not noted in the Workbench log since "log_term_creation" is set to false).'
                    print(message)
                else:
                    message = "Skipping check for existence of terms (note: new terms will be created "
                    if config["log_term_creation"] is True:
                        message += "as noted in the Workbench log)."
                    else:
                        message += 'but not noted in the Workbench log - "log_term_creation" is set to false).'
                    print(message)
                    logging.warning(
                        "Skipping check for existence of terms (but new terms will be created)."
                    )
            elif not new_terms:
                # All term IDs are in their field's vocabularies.
                print("OK, term IDs/names in CSV file exist in their respective taxonomies.")
                logging.info(
                    "OK, term IDs/names in CSV file exist in their respective taxonomies."
                )

        if len(typed_relation_reference_fields_present) > 0:
            for typed_relation_reference_field in typed_relation_reference_fields_present:
                if len(row[typed_relation_reference_field]) > 0:
                    delimited_field_values = row[typed_relation_reference_field].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0:
                            try:
                                workbench_utils.validate_typed_relation_field_value_action(
                                    config, field_definitions, typed_relation_reference_field, field_value.strip(), row[config["id_field"]]
                                )
                            except WorkbenchValidationException as e:
                                message = (
                                    f'Value in field "{typed_relation_reference_field}" in row with ID {row[config["id_field"]]} (' +
                                    f'{field_value}) is not a valid typed relation reference field value: {str(e)}'
                                )
                                row_errors.append((count, message))



    if len(row_errors) > 0:
        error_messages = "\n".join([f"Row {row_num}: {error_message}" for row_num, error_message in row_errors])
        logging.error(f"Errors found in CSV data:\n{error_messages}")
        sys.exit(f"Error: Errors found in CSV data. See log for more detail.\n{error_messages}")
    else:
        if is_geolocation_fields_present:
            message = "OK, geolocation field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_link_fields_present:
            message = "OK, link field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_authority_link_fields_present:
            message = "OK, authority link field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_edtf_fields_present:
            message = "OK, EDTF field values in the CSV file validate."
            print(message)
            logging.info(message)


def _task_specific_checks_update_terms(config: dict, reader: workbench_utils.WorkbenchCsvReader, csv_column_headers: list):
    """Checks specific to "update_terms" tasks.
    Parameters
    :param config: dict - the configuration dictionary
    :param reader: WorkbenchCsvReader - the CSV reader object
    :param csv_column_headers: list - the list of CSV column headers
    """
    field_definitions = workbench_utils.get_field_definitions(
        config, "taxonomy_term", config["vocab_id"]
    )
    term_base_fields = ["status", "langcode", "term_name", "parent", "weight", "description", "published", ]
    drupal_fieldnames = list(field_definitions.keys())
    """
    if "term_name" in csv_column_headers:
        csv_column_headers.remove("term_name")
    if "parent" in csv_column_headers:
        csv_column_headers.remove("parent")
    if "weight" in csv_column_headers:
        csv_column_headers.remove("weight")
    if "description" in csv_column_headers:
        csv_column_headers.remove("description")
    if "term_id" in csv_column_headers:
        csv_column_headers.remove("term_id")
    """

    for csv_column_header in csv_column_headers:
        if (
                csv_column_header not in drupal_fieldnames and csv_column_header != "term_id" and csv_column_header not in term_base_fields):
            message = f'CSV column header "{csv_column_header}" does not match any Drupal field names in the {config["vocab_id"]} vocabulary.'
            logging.error(message)
            sys.exit("Error: " + message)
    message = "OK, CSV column headers match Drupal field names."
    print(message)
    logging.info(message)

    # Generate a list of geolocation fields
    geolocation_fields = [ key for key, val in field_definitions.items() if val["field_type"] == "geolocation" ]
    # Generate a list of geolocation fields that are present in the CSV file.
    geolocation_fields_present = [ field for field in geolocation_fields if field in reader.get_field_names() ]
    # Set this to True if we find any geolocation fields in the CSV file, so we can report at the end of the checks whether geolocation field validation was performed.
    is_geolocation_fields_present = False

    # Generate a list of link fields
    link_fields = [key for key, val in field_definitions.items() if val["field_type"] == "link"]
    # Generate a list of link fields that are present in the CSV file.
    link_fields_present = [field for field in link_fields if field in reader.get_field_names()]
    # Set this to True if we find any link fields in the CSV file, so we can report at the end of the checks whether link field validation was performed.
    is_link_fields_present = False

    # Generate a list of authority link fields
    authority_link_fields = [key for key, val in field_definitions.items() if val["field_type"] == "authority_link"]
    # Generate a list of authority link fields that are present in the CSV file.
    authority_link_fields_present = [field for field in authority_link_fields if field in reader.get_field_names()]
    # Set this to True if we find any authority link fields in the CSV file, so we can report at the end of the checks whether authority link field validation was performed.
    is_authority_link_fields_present = False

    # Generate a list of EDTF fields
    edtf_fields = [key for key, val in field_definitions.items() if val["field_type"] == "edtf"]
    # Generate a list of EDTF fields that are present in the CSV file.
    edtf_fields_present = [field for field in edtf_fields if field in reader.get_field_names()]
    # Set this to True if we find any EDTF fields in the CSV file, so we can report at the end of the checks whether EDTF field validation was performed.
    is_edtf_fields_present = False

    # Generate a list of fields with cardinality > 1 that are present in the CSV file.
    cardinality_fields_present = {key: field_definitions[key]["cardinality"] for key in csv_column_headers if
                                  field_definitions[key]["cardinality"] > 0 and key != "title"}

    taxonomy_reference_fields = [key for key, val in field_definitions.items() if
                                 val["field_type"] == "entity_reference" and val["target_type"] == "taxonomy_term"]
    taxonomy_reference_fields_present = [field for field in taxonomy_reference_fields if
                                         field in reader.get_field_names()]
    taxonomy_reference_errors = []
    for taxonomy_reference_field in taxonomy_reference_fields_present:
        try:
            vocabularies = workbench_utils.get_field_vocabularies(
                config, field_definitions, taxonomy_reference_field
            )
            _ = len(vocabularies)
        except TypeError:
            message = (
                f'Workbench cannot get vocabularies linked to field "{taxonomy_reference_field}". Please confirm that field has at least one vocabulary.')
            taxonomy_reference_errors.append(message)
    if len(taxonomy_reference_errors) > 0:
        error_messages = "\n".join(taxonomy_reference_errors)
        logging.error(f"Errors found in taxonomy reference field configuration:\n{error_messages}")
        sys.exit(
            f"Error: Errors found in taxonomy reference field configuration. See log for more detail.\n{error_messages}"
            )

    typed_relation_reference_fields = [key for key, val in field_definitions.items() if
                                       val["field_type"] == "typed_relation" and "typed_relations" in val[key]]
    typed_relation_reference_fields_present = [field for field in typed_relation_reference_fields if
                                               field in reader.get_field_names()]

    # Validate length of 'term_name'.
    row_errors = []
    for count, row in enumerate(reader.get_csv_data(), start=1):
        if "term_name" in row and len(row["term_name"]) > 255:
            message = ("The 'term_name' column in row for term '" + row[
                "term_name"] + "' of your CSV file exceeds Drupal's maximum length of 255 characters.")
            row_errors.append((count, message))

        if len(geolocation_fields_present) > 0:
            for geolocation_field in geolocation_fields_present:
                if geolocation_field in row and len(row[geolocation_field]) > 0:
                    is_geolocation_fields_present = True
                    try:
                        workbench_utils.validate_geolocation_fields_action(config, row, geolocation_field)
                    except WorkbenchValidationException as e:
                        row_errors.append((count, str(e)))

        if len(link_fields_present) > 0:
            for link_field in link_fields_present:
                if link_field in row and len(row[link_field]) > 0:
                    link_value = row[link_field].strip()
                    is_link_fields_present = True
                    if not workbench_utils.validate_link_value(link_value):
                        message = (
                            f'Value in field "{link_field}" in row with ID {row[config["id_field"]]} (' +
                            f'{link_value}) is not a valid link field value.'
                        )
                        row_errors.append((count, message))

        if len(authority_link_fields_present) > 0:
            for authority_link_field in authority_link_fields_present:
                if authority_link_field in row and len(row[authority_link_field]) > 0:
                    is_authority_link_fields_present = True
                    authority_link_value = row[authority_link_field].strip()
                    delimited_field_values = authority_link_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if (len(field_value.strip()) > 0 and
                                not workbench_utils.validate_authority_link_value(
                                    field_value.strip(),
                                    field_definitions[authority_link_field]["authority_sources"],
                        )):
                            message = (
                                f'Value in field "{authority_link_field}" in row with ID {row[config["id_field"]]} (' +
                                f'{field_value}) is not a valid authority link field value.'
                            )
                            row_errors.append((count, message))

        if len(edtf_fields_present) > 0:
            for edtf_field in edtf_fields_present:
                if edtf_field in row and len(row[edtf_field]) > 0:
                    is_edtf_fields_present = True
                    edtf_value = row[edtf_field].strip()
                    delimited_field_values = edtf_value.split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0 and not workbench_utils.validate_edtf_date(field_value.strip()):
                            message = (
                                f'Value in field "{edtf_field}" in row with ID {row["node_id"]} (' +
                                f'{field_value}) is not a valid EDTF date/time.'
                            )
                            row_errors.append((count, message))

        if len(cardinality_fields_present) > 0:
            for cardinality_field, cardinality_value in cardinality_fields_present:
                delimited_field_values = row[cardinality_field].split(config["subdelimiter"])
                if len(delimited_field_values) > cardinality_value:
                    message = (f'CSV field "{cardinality_field}" in record with ID {row["node_id"]} contains more values ' +
                    f'than the number of values allowed for that field (max {cardinality_value}). Workbench with add only the first{"" if cardinality_value == 1 else f" {cardinality_value}"} value(s) for that field.')
                    logging.warning(message)
                    print(f"Warning: {message}")

        for header in csv_column_headers:
            if "max_length" in field_definitions[header] and field_definitions[header]["max_length"] is not None:
                delimited_field_values = row[header].split(config["subdelimiter"])
                for field_value in delimited_field_values:
                    if len(field_value) > int(field_definitions[header]["max_length"]):
                        message = (f'CSV field "{header}" in record with ID {row["node_id"]} contains a value that is ' +
                                   f'longer ({str(len(field_value))} characters) than allowed for that field ({str(field_definitions[header]["max_length"])}) ' +
                                    ' characters). Workbench will truncate this value prior to populating Drupal.')
                        logging.warning(message)
                        print(f"Warning: {message}")

        if len(taxonomy_reference_fields_present) > 0:
            new_terms: bool = False
            for taxonomy_reference_field in taxonomy_reference_fields_present:
                if len(row[taxonomy_reference_field]) > 0:
                    try:
                        new_terms = workbench_utils.validate_taxonomy_reference_value_action(config, field_definitions, taxonomy_reference_field, row[taxonomy_reference_field], row[config["id_field"]])
                    except WorkbenchValidationException as e:
                        if hasattr(e, "wrapped"):
                            # Wrapped exception is the compilation of one or more validation errors for the individual values in a multi-valued typed relation field.
                            row_errors.append((count, str(e)))
            if new_terms and config["allow_adding_terms"]:
                if config["validate_terms_exist"]:
                    message = "OK, term IDs/names in CSV file exist in their respective taxonomies (new terms will be created"
                    if config["log_term_creation"] is True:
                        message += " as noted in the Workbench log)."
                    else:
                        message += ' but not noted in the Workbench log since "log_term_creation" is set to false).'
                    print(message)
                else:
                    message = "Skipping check for existence of terms (note: new terms will be created "
                    if config["log_term_creation"] is True:
                        message += "as noted in the Workbench log)."
                    else:
                        message += 'but not noted in the Workbench log - "log_term_creation" is set to false).'
                    print(message)
                    logging.warning(
                        "Skipping check for existence of terms (but new terms will be created)."
                    )
            elif not new_terms:
                # All term IDs are in their field's vocabularies.
                print("OK, term IDs/names in CSV file exist in their respective taxonomies.")
                logging.info(
                    "OK, term IDs/names in CSV file exist in their respective taxonomies."
                )

        if len(typed_relation_reference_fields_present) > 0:
            for typed_relation_reference_field in typed_relation_reference_fields_present:
                if len(row[typed_relation_reference_field]) > 0:
                    delimited_field_values = row[typed_relation_reference_field].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()) > 0:
                            try:
                                workbench_utils.validate_typed_relation_field_value_action(
                                    config, field_definitions, typed_relation_reference_field, field_value.strip(), row[config["id_field"]]
                                )
                            except WorkbenchValidationException as e:
                                if hasattr(e, "wrapped"):
                                    # Wrapped exception is the compilation of one or more validation errors for the individual values in a multi-valued typed relation field.
                                    row_errors.append((count, str(e)))
                                else:
                                    message = (
                                        f'Value in field "{typed_relation_reference_field}" in row with ID {row[config["id_field"]]} (' +
                                        f'{field_value}) is not a valid typed relation reference field value: {str(e)}'
                                    )
                                    row_errors.append((count, message))

    if len(row_errors) > 0:
        error_messages = "\n".join([f"Row {row_num}: {error_message}" for row_num, error_message in row_errors])
        logging.error(f"Errors found in CSV data:\n{error_messages}")
        sys.exit(f"Error: Errors found in CSV data. See log for more detail.\n{error_messages}")
    else:
        if is_geolocation_fields_present:
            message = "OK, geolocation field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_link_fields_present:
            message = "OK, link field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_authority_link_fields_present:
            message = "OK, authority link field values in the CSV file validate."
            print(message)
            logging.info(message)
        if is_edtf_fields_present:
            message = "OK, EDTF field values in the CSV file validate."
            print(message)
            logging.info(message)

def _task_specific_checks_add_or_update_alt_text(config: dict, reader: workbench_utils.WorkbenchCsvReader):
    """Checks specific to "add_alt_text" or "update_alt_text" tasks.
    Parameters
    :param config: dict - the configuration dictionary
    :param reader: WorkbenchCsvReader - the CSV reader object
    """
    _alt_text_required_options = ["task", "host", "username", "password"]
    missing_options = [ x for x in _alt_text_required_options if x not in config.keys() ]
    if len(missing_options) > 0:
            message = ("Please check your config file for required values: " + ", ".join(
                _alt_text_required_options
                ) + ".")
            logging.error(message)
            sys.exit("Error: " + message)
    update_mode_options = ["replace", "append", "delete"]
    if config["update_mode"] not in update_mode_options:
        message = ('Your "update_mode" config option must be one of the following: ' + ", ".join(
            update_mode_options
            ) + ".")
        logging.error(message)
        sys.exit("Error: " + message)

    row_errors = []
    row_warnings = []
    for row_counter, row in enumerate(reader.get_csv_data(), start=1):
        if len(row["node_id"]) > 0:
            node_id = row["node_id"]
            parent_node_exists = workbench_utils.ping_node(config, row["node_id"], warn=False)
            if parent_node_exists is False:
                message = f'Node identified in "node_id" ({node_id}) in row "{row_counter}" of your input CSV cannot be found or accessed.'
                row_errors.append((row_counter, message))
        else:
            message = f"Row {row_counter} in your input CSV file is empty."
            row_errors.append((row_counter, message))

        if len(row["image_alt_text"]) > config["max_image_alt_text_length"]:
            image_alt_text = row["image_alt_text"]
            max_alt_text_length = config["max_image_alt_text_length"]
            node_id = row["node_id"]
            message = f"Alt text in input CSV row with node ID {node_id} is longer than the maximum configured alt text length ({max_alt_text_length})"
            message += f" (length is {len(image_alt_text)} characters). This row will be skipped."
            row_warnings.append((row_counter, message))

    if len(row_warnings) > 0:
        warning_messages = "\n".join([f"Row {row_num}: {warning_message}" for row_num, warning_message in row_warnings])
        logging.warning(f"Warnings found in CSV data:\n{warning_messages}")
        print(f"Warning: Warnings found in CSV data. See log for more detail.\n{warning_messages}")

    if len(row_errors) > 0:
        error_messages = "\n".join([f"Row {row_num}: {error_message}" for row_num, error_message in row_errors])
        logging.error(f"Errors found in CSV data:\n{error_messages}")
        sys.exit(f"Error: Errors found in CSV data. See log for more detail.\n{error_messages}")

def check_input(config: dict, args: Namespace) -> None:
    # TODO: This function is very important and also over 2400 lines long. Can it be refactored into smaller pieces?
    """Validate the config file and input data.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    args: ArgumentParser
        Command-line arguments from argparse.parse_args().
    Returns
    -------
    None
        Exits if an error is encountered.
    """
    if config["task"] == "update":
        update_mode_string = f' ({config["update_mode"]})'
    else:
        update_mode_string = ""

    logging.info(
        f'Starting configuration check for "%s" task using config file %s.',
        config["task"] + update_mode_string,
        args.config,
    )

    if "check_lock_file_path" in config:
        if os.path.exists(config["check_lock_file_path"]):
            os.remove(config["check_lock_file_path"])

    ping_islandora(config, print_message=False)
    check_integration_module_version(config, log_success=False)

    rows_with_missing_files = list()
    csv_reader = WorkbenchCsvReader(config)

    # @todo #606: break out node entity and reserved field, media entity and reserved field, and term entity and reserved fields?
    node_base_fields = [
        "title",
        "status",
        "promote",
        "sticky",
        "uid",
        "created",
        "published",
    ]
    # Any new reserved columns introduced into the CSV need to be removed here. 'langcode' is a standard Drupal field
    # but it doesn't show up in any field configs.
    reserved_fields = [
        "file",
        "directory",
        "media_use_tid",
        "checksum",
        "node_id",
        "url_alias",
        "image_alt_text",
        "parent_id",
        "langcode",
        "revision_log",
    ]
    entity_fields = get_entity_fields(config, "node", config["content_type"])
    if config["id_field"] not in entity_fields:
        reserved_fields.append(config["id_field"])

    config_keys = list(config.keys())
    config_keys.remove("check")

    if config["task"] in ["create", "create_from_files"]:
        _simple_config_checks(config)
        check_for_parent_csv_headers = csv_reader.get_field_names()

        _check_host_values(config, check_for_parent_csv_headers)
        # Check that the rollback configuration file and CSV file directories exist and are writable.
        check_rollback_file_path_directories(config)
    ##### END IF TASK == CREATE, CREATE FROM FILES

    # Check for presence of required config keys, which varies by task.
    if config["task"] == "create":
        if config["nodes_only"] is True:
            message = '"nodes_only" option in effect. Media files will not be checked/validated.'
            print(message)
            logging.info(message)
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password"]
        )
    elif config["task"] == "update":
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password"]
        )
        update_mode_options = ["replace", "append", "delete"]
        if config["update_mode"] not in update_mode_options:
            message = (
                'Your "update_mode" config option must be one of the following: '
                + ", ".join(update_mode_options)
                + "."
            )
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] == "delete":
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password"]
        )
    elif config["task"] == "add_media":
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password", "media_type"]
        )
    elif config["task"] in ["update_media", "update_media_by_node"]:
        check_for_required_config_keys(
            config_keys,
            ["task", "host", "username", "password", "input_csv", "media_type"],
        )
        update_mode_options = ["replace", "append", "delete"]
        if config["update_mode"] not in update_mode_options:
            message = (
                'Your "update_mode" config option must be one of the following: '
                + ", ".join(update_mode_options)
                + "."
            )
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] == "delete_media":
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password"]
        )
    elif config["task"] == "delete_media_by_node":
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password"]
        )
    elif config["task"] == "export_csv":
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password"]
        )
        if config["export_csv_term_mode"] == "name":
            message = 'The "export_csv_term_mode" configuration option is set to "name", which will slow down the export.'
            print(message)
    elif config["task"] == "create_terms":
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password", "vocab_id"]
        )
    elif config["task"] in ["get_data_from_view", "get_media_report_from_view"]:
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password", "view_path"]
        )
    elif config["task"] == "update_terms":
        check_for_required_config_keys(
            config_keys, ["task", "host", "username", "password", "vocab_id"]
        )

    message = "OK, configuration file has all required values (did not check for optional values)."
    print(message)
    logging.info(message)

    create_temp_dir(config)

    # Perform checks on get_data_from_view tasks. Since this task doesn't use input_dir, input_csv, etc.,
    # we exit immediately after doing these checks.
    if (
        config["task"] == "get_data_from_view"
        or config["task"] == "get_media_report_from_view"
    ):
        _check_get_data_from_view(config, args.config)
        sys.exit()
    ### END IF TASK == GET_DATA_FROM_VIEW, GET_MEDIA_REPORT_FROM_VIEW

    validate_input_dir(config)

    check_csv_file_exists(config, "node_fields")

    # Check column headers in CSV file. Does not apply to add_media or update_media/update_media_by_node tasks (handled just below).
    csv_reader = WorkbenchCsvReader(config)

    langcode_was_present = False



    if config["csv_headers"] == "labels" and config["task"] in [
        "create",
        "update",
        "create_terms",
        "update_terms",
    ]:
        mapname_part = f"taxonomy_term-{config['vocab_id']}" if config["task"] == "create_terms" or config["task"] == "update_terms" else f"node-{config['content_type']}"
        fieldname_map_cache_path = os.path.join(
            config["temp_dir"], f"{mapname_part}-labels.fieldname_map", )
        if os.path.exists(fieldname_map_cache_path):
            os.remove(fieldname_map_cache_path)
        csv_column_headers = replace_field_labels_with_names(
            config, csv_reader.get_field_names()
        )
    else:
        csv_column_headers = csv_reader.get_field_names()

    if "langcode" in csv_column_headers:
        langcode_was_present = True

    if config["task"] in ["add_media", "update_media", "update_media_by_node"]:
       _check_media_csv_headers(config, csv_column_headers)
    ### END IF TASK == ADD_MEDIA, UPDATE_MEDIA, UPDATE_MEDIA_BY_NODE

    # Check whether each row contains the same number of columns as there are headers.
    # This check has been moved to WorkbenchCsvReader._generate_preprocessed_csv() and would have
    # thrown an exception above, so just print the message
    message = (
        "OK, all "
        + str(csv_reader.get_row_count())
        + " rows in the CSV file have the same number of columns as there are headers ("
        + str(len(csv_column_headers))
        + ")."
    )
    print(message)
    logging.info(message)

    # Check for presence of CSV filters
    row_filter_settings = []
    if config["csv_start_row"] != 0:
        row_filter_settings.append("csv_start_row")
    if config["csv_stop_row"] is not None:
        row_filter_settings.append("csv_stop_row")
    if "csv_rows_to_process" in config:
        row_filter_settings.append("csv_rows_to_process")
    if "csv_row_filters" in config:
        row_filter_settings.append("csv_row_filters")
    if csv_reader.has_commented_out_rows():
        row_filter_settings.append(True)
    if len(row_filter_settings) > 1:
        preprocessed_input_csv_file_path = csv_reader.get_preprocessed_csv_filepath()
        message = f'Your configuration contains more than one input CSV row filter setting, and/or your input CSV has some commented-out rows. Please check "{preprocessed_input_csv_file_path}" to confirm the rows you want are present.'
        logging.warning(message)

    # Check existence of input data zip archives.
    if len(config["input_data_zip_archives"]) > 0:
        for input_data_zip_archive_location in config["input_data_zip_archives"]:
            if input_data_zip_archive_location.lower().startswith("http"):
                remote_zip_archive_ping_response_code = ping_remote_file(
                    config, input_data_zip_archive_location
                )
                if remote_zip_archive_ping_response_code != 200:
                    message = f'Remote input data zip archive "{input_data_zip_archive_location}" not found, ping returned a {remote_zip_archive_ping_response_code} response.'
                    print("Warning: " + message)
                    logging.warning(message)
            else:
                if os.path.exists(input_data_zip_archive_location):
                    message = f'Local input data zip archive "{input_data_zip_archive_location}" found.'
                    print("Ok, " + message)
                    logging.info(message)
                else:
                    message = f'Local input data zip archive "{input_data_zip_archive_location}" not found.'
                    print("Warning: " + message)
                    logging.warning(message)
    ### END IF INPUT_DATA_ZIP_ARCHIVES

    task_specific_checks(config, csv_reader, reserved_fields)

    # END IF TASK == CREATE_TERMS or UPDATE_TERMS

    if config["task"] == "update" or config["task"] == "create":
        field_definitions = get_field_definitions(config, "node")

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_csv_field_length(config, field_definitions, csv_reader)

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_text_list_fields(config, field_definitions, csv_reader)

        validate_numeric_fields_data = csv_reader.get_csv_data()
        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_numeric_fields(config, field_definitions, validate_numeric_fields_data)

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_media_track_fields(config, csv_reader)

        # Validate existence of nodes specified in 'field_member_of'. This could be generalized out to validate node IDs in other fields.
        # See https://github.com/mjordan/islandora_workbench/issues/90.
        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        if config["validate_parent_node_exists"] is True:
            validate_field_member_of_csv_data = csv_reader.get_csv_data()
            for count, row in enumerate(validate_field_member_of_csv_data, start=1):
                if "field_member_of" in csv_column_headers:
                    parent_nids = row["field_member_of"].split(config["subdelimiter"])
                    for parent_nid in parent_nids:
                        if len(parent_nid) > 0:
                            parent_node_exists = ping_node(
                                config, parent_nid, warn=False
                            )
                            if parent_node_exists is False:
                                message = (
                                    "The 'field_member_of' field in row with ID '"
                                    + row[config["id_field"]]
                                    + "' of your CSV file contains a node ID ("
                                    + parent_nid
                                    + ") that "
                                    + "doesn't exist or is not accessible. See the workbench log for more information."
                                )
                                message = f'Node identified in "field_member_of" ({parent_nid}) in row with ID "{row[config["id_field"]]}" cannot be found or accessed.'
                                logging.error(message)
                                sys.exit(
                                    "Error: "
                                    + message
                                    + " See Workbench log for more information."
                                )
        else:
            message = (
                '"validate_parent_node_exists" is set to false. Node IDs in "field_member_of" that do not exist or are not accessible '
                + 'will result in 422 errors in "create" and "update" tasks.'
            )
            logging.warning(message)

        # Check the configuration that is necessary for enabling use of term names in Entity Reference Views fields.
        if "entity_reference_view_endpoints" in config:
            entity_reference_view_endpoints = get_entity_reference_view_endpoints(
                config
            )
            for (
                entity_reference_view_field_name,
                entity_reference_view_endpoint,
            ) in entity_reference_view_endpoints.items():
                if entity_reference_view_field_name not in csv_column_headers:
                    message = f'CSV column {entity_reference_view_field_name} identified in "entity_reference_view_endpoints" is not in your CSV file.'
                    logging.error(message)
                    sys.exit("Error: " + message)
                view_url = (
                    f'{config["host"]}/{entity_reference_view_endpoint.lstrip("/")}'
                )
                view_path_status_code = ping_view_endpoint(config, view_url)
                if view_path_status_code != 200:
                    message = f'Cannot access View REST export configured in "entity_reference_view_endpoints" ({view_url}).'
                    logging.error(message)
                    sys.exit("Error: " + message)
                else:
                    message = f'View REST export configured in "entity_reference_view_endpoints" ({view_url}) is accessible.'
                    logging.info(message)
                    print("OK, " + message)

        # Validate 'langcode' values if that field exists in the CSV.
        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        if langcode_was_present:
            validate_langcode_csv_data = csv_reader.get_csv_data()
            for count, row in enumerate(validate_langcode_csv_data, start=1):
                langcode_valid = validate_language_code(row["langcode"])
                if not langcode_valid:
                    message = (
                        "Row with ID "
                        + row[config["id_field"]]
                        + " of your CSV file contains an invalid Drupal language code ("
                        + row["langcode"]
                        + ") in its 'langcode' column."
                    )
                    logging.error(message)
                    sys.exit("Error: " + message)
    ### END IF TASK == UPDATE or CREATE

    if config["task"] == "delete":
        if "node_id" not in csv_column_headers:
            message = (
                'For "delete" tasks, your CSV file must contain a "node_id" column.'
            )
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] == "add_media":
        if "node_id" not in csv_column_headers:
            message = (
                'For "add_media" tasks, your CSV file must contain a "node_id" column.'
            )
            logging.error(message)
            sys.exit("Error: " + message)
        elif "file" not in csv_column_headers:
            message = (
                'For "add_media" tasks, your CSV file must contain a "file" column.'
            )
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] == "update_media":
        if "media_id" not in csv_column_headers:
            message = 'For "update_media" tasks, your CSV file must contain a "media_id" column.'
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] == "update_media_by_node":
        if "node_id" not in csv_column_headers:
            message = 'For "update_media_by_node" tasks, your CSV file must contain a "node_id" column.'
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] == "delete_media":
        if "media_id" not in csv_column_headers:
            message = 'For "delete_media" tasks, your CSV file must contain a "media_id" column.'
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] == "delete_media_by_node":
        if "node_id" not in csv_column_headers:
            message = 'For "delete_media_by_node" tasks, your CSV file must contain a "node_id" column.'
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] == "update_terms":
        if "term_id" not in csv_column_headers:
            message = 'For "update_terms" tasks, your CSV file must contain a "term_id" column.'
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] == "create_redirects":
        if "redirect_source" not in csv_column_headers:
            message = 'For "create_redirects" tasks, your CSV file must contain a "redirect_source" column.'
            logging.error(message)
            sys.exit("Error: " + message)
        if "redirect_target" not in csv_column_headers:
            message = 'For "create_redirects" tasks, your CSV file must contain a "redirect_target" column.'
            logging.error(message)
            sys.exit("Error: " + message)
    elif config["task"] in ["add_alt_text", "update_alt_text"]:
        if "node_id" not in csv_column_headers:
            t = config["task"]
            message = f'For "{t}" tasks, your CSV file must contain a "node_id" column.'
            logging.error(message)
            sys.exit("Error: " + message)
        if "image_alt_text" not in csv_column_headers:
            message = f'For "{t}" tasks, your CSV file must contain a "image_alt_text" column.'
            logging.error(message)
            sys.exit("Error: " + message)

    warnings_about_redirect_input_csv = False
    if config["task"] == "create_redirects":
        # Ping /entity/redirect and expect a 405 response.
        endpoint_ping_response = requests.head(
            config["host"].rstrip("/") + "/entity/redirect?_format=json",
            allow_redirects=True,
            verify=config["secure_ssl_only"],
            auth=(config["username"], config["password"]),
        )
        if endpoint_ping_response.status_code != 405:
            message = (
                'Cannot access "'
                + config["host"].rstrip("/")
                + "/entity/redirect"
                + '". Please confirm that the "Redirect" REST endpoint is configured properly.'
            )
            logging.error(message)
            sys.exit("Error: " + message)

        check_for_redirects_csv_data = csv_reader.get_csv_data()
        for count, row in enumerate(check_for_redirects_csv_data, start=1):
            if len(row["redirect_source"].strip()) == 0:
                message = f"Redirect source value in input CSV row {count} is empty. Redirect will not be created."
                logging.warning(message)
                warnings_about_redirect_input_csv = True
                continue

            if len(row["redirect_target"].strip()) == 0:
                message = f"Redirect target value in input CSV row {count} is empty. Redirect will not be created."
                logging.warning(message)
                warnings_about_redirect_input_csv = True
                continue

            if row["redirect_source"].lower().startswith("http"):
                message = (
                    'Redirect source values cannot contain a hostname, they must be a path only, without a hostname. Please correct "'
                    + row["redirect_source"]
                    + " (row "
                    + str(count)
                    + ")."
                )
                logging.warning(message)
                warnings_about_redirect_input_csv = True
                continue

            # Check to see if the redirect source value is already a redirect. We don't use issue_request()
            # since we don't want to override config["allow_redirects"] for this one request.
            is_redirect_url = config["host"].rstrip("/") + "/" + row["redirect_source"]
            is_redirect_response = requests.head(
                is_redirect_url,
                allow_redirects=False,
                verify=config["secure_ssl_only"],
                auth=(config["username"], config["password"]),
            )
            if str(is_redirect_response.status_code).startswith("30"):
                message = (
                    'Redirect source path "'
                    + row["redirect_source"].strip()
                    + '" (row '
                    + str(count)
                    + ') is already a redirect to "'
                    + is_redirect_response.headers["Location"]
                    + '" (HTTP response code is '
                    + str(is_redirect_response.status_code)
                    + ")."
                )
                logging.warning(message)
                warnings_about_redirect_input_csv = True
                continue

            # Log whether the source path exists. We don't use issue_request() since we
            # don't want to override config["allow_redirects"] for this one request.
            path_exists_url = config["host"].rstrip("/") + "/" + row["redirect_source"]
            path_exists_response = requests.head(
                path_exists_url,
                allow_redirects=False,
                verify=config["secure_ssl_only"],
                auth=(config["username"], config["password"]),
            )
            if path_exists_response.status_code == 404:
                message = (
                    'Redirect source path "'
                    + row["redirect_source"].strip()
                    + '" (row '
                    + str(count)
                    + ") does not exist (HTTP response code is "
                    + str(path_exists_response.status_code)
                    + ") so is available as a redirect."
                )
                logging.info(message)
                continue
            else:
                # We've already tested for 3xx responses, so assume that the path exists.
                message = (
                    'Redirect source path "'
                    + row["redirect_source"].strip()
                    + '" (row '
                    + str(count)
                    + ") already exists."
                )
                logging.warning(message)
                warnings_about_redirect_input_csv = True
                continue

        if warnings_about_redirect_input_csv is True:
            message = (
                "Input CSV contains at least one row that has generated a warning."
            )
            print("Warning: " + message + " See the log for details.")
    # END IF TASK == CREATE_REDIRECTS

    # Check for existence of files listed in the 'file' column.
    if (
        config["task"] == "create"
        or config["task"] == "add_media"
        or config["task"] == "update_media"
        or config["task"] == "update_media_by_node"
        and "file" in csv_column_headers
    ):
        if config["nodes_only"] is False and (
            config["paged_content_from_directories"] is False
            or config["paged_content_from_directories_parents_exist"] is False
        ):
            # Temporary fix for https://github.com/mjordan/islandora_workbench/issues/478.
            if config["task"] == "add_media":
                config["id_field"] = "node_id"
            if config["task"] == "update_media":
                config["id_field"] = "media_id"
            if config["task"] == "update_media_by_node":
                config["id_field"] = "node_id"

            file_check_csv_data = csv_reader.get_csv_data()
            for count, file_check_row in enumerate(file_check_csv_data, start=1):
                file_check_row["file"] = file_check_row["file"].strip()
                # Check for and log empty 'file' values.
                if len(file_check_row["file"]) == 0:
                    message = (
                        "CSV row with ID "
                        + file_check_row[config["id_field"]]
                        + ' contains an empty "file" value.'
                    )
                    logging.warning(message)

                # Check for files that cannot be found.
                if (
                    not file_check_row["file"].startswith("http")
                    and len(file_check_row["file"].strip()) > 0
                ):
                    if os.path.isabs(file_check_row["file"]):
                        file_path = file_check_row["file"]
                    else:
                        file_path = os.path.join(
                            config["input_dir"], file_check_row["file"]
                        )
                    if not os.path.exists(file_path) or not os.path.isfile(file_path):
                        message = (
                            'File "'
                            + file_path
                            + '" identified in CSV "file" column for row with ID "'
                            + file_check_row[config["id_field"]]
                            + '" not found.'
                        )
                        if config["allow_missing_files"] is False:
                            logging.error(message)
                            if config["perform_soft_checks"] is False:
                                sys.exit("Error: " + message)
                            else:
                                if (
                                    file_check_row[config["id_field"]]
                                    not in rows_with_missing_files
                                    and len(file_check_row["file"].strip()) > 0
                                ):
                                    rows_with_missing_files.append(
                                        file_check_row[config["id_field"]]
                                    )
                        else:
                            logging.error(message)
                            if (
                                file_check_row[config["id_field"]]
                                not in rows_with_missing_files
                                and len(file_check_row["file"].strip()) > 0
                            ):
                                rows_with_missing_files.append(
                                    file_check_row[config["id_field"]]
                                )
                # Remote files.
                else:
                    if len(file_check_row["file"].strip()) > 0:
                        http_response_code = ping_remote_file(
                            config, file_check_row["file"]
                        )
                        if (
                            http_response_code != 200
                            or ping_remote_file(config, file_check_row["file"]) is False
                        ):
                            message = (
                                'Remote file "'
                                + file_check_row["file"]
                                + '" identified in CSV "file" column for row with ID "'
                                + file_check_row[config["id_field"]]
                                + '" not found or not accessible (HTTP response code '
                                + str(http_response_code)
                                + ")."
                            )
                            if config["allow_missing_files"] is False:
                                logging.error(message)
                                if config["perform_soft_checks"] is False:
                                    sys.exit("Error: " + message)
                                else:
                                    if (
                                        file_check_row[config["id_field"]]
                                        not in rows_with_missing_files
                                        and len(file_check_row["file"].strip()) > 0
                                    ):
                                        rows_with_missing_files.append(
                                            file_check_row[config["id_field"]]
                                        )
                            else:
                                logging.error(message)
                                if (
                                    file_check_row[config["id_field"]]
                                    not in rows_with_missing_files
                                    and len(file_check_row["file"].strip()) > 0
                                ):
                                    rows_with_missing_files.append(
                                        file_check_row[config["id_field"]]
                                    )

            # @todo for issue 268: All accumulator variables like 'rows_with_missing_files' should be checked at end of
            # check_input() (to work with perform_soft_checks: True) in addition to at place of check (to work wit perform_soft_checks: False).
            if len(rows_with_missing_files) > 0:
                if config["allow_missing_files"] is True:
                    message = '"allow_missing_files" configuration setting is set to "true", and CSV "file" column values containing missing files were detected.'
                    print("Warning: " + message + " See the log for more information.")
                    logging.warning(message + " Details are logged above.")
            else:
                message = 'OK, files named in the CSV "file" column are all present.'
                print(message)
                logging.info(message)

            # Verify that all media bundles/types exist.
            if config["nodes_only"] is False:
                media_type_check_csv_data = csv_reader.get_csv_data()
                for count, file_check_row in enumerate(
                    media_type_check_csv_data, start=1
                ):
                    filename_fields_to_check = ["file"]
                    for filename_field in filename_fields_to_check:
                        if len(file_check_row[filename_field]) != 0:
                            media_type = set_media_type(
                                config,
                                file_check_row[filename_field],
                                filename_field,
                                file_check_row,
                            )
                            media_bundle_response_code = ping_media_bundle(
                                config, media_type
                            )
                            if media_bundle_response_code == 404:
                                message = (
                                    'File "'
                                    + file_check_row[filename_field]
                                    + '" identified in CSV row '
                                    + file_check_row[config["id_field"]]
                                    + " will create a media of type ("
                                    + media_type
                                    + "), but that media type is not configured in the destination Drupal."
                                    + " Please make sure your media type configuration matches your Drupal configuration."
                                )
                                logging.error(message)
                                sys.exit("Error: " + message)

                            # Check that each file's extension is allowed for the current media type. 'file' is the only
                            # CSV field to check here. Files added using the 'additional_files' setting are checked below.
                            if file_check_row["file"].startswith("http"):
                                # First check to see if the file has an extension.
                                extension = os.path.splitext(file_check_row["file"])[1]
                                if len(extension) > 0:
                                    extension = extension.lstrip(".").lower()
                                else:
                                    extension = get_remote_file_extension(
                                        config, file_check_row["file"]
                                    )
                                    extension = extension.lstrip(".")
                            else:
                                extension = os.path.splitext(file_check_row["file"])[1]
                                extension = extension.lstrip(".").lower()
                            media_type_file_field = config["media_type_file_fields"][
                                media_type
                            ]
                            registered_extensions = get_registered_media_extensions(
                                config, media_type, media_type_file_field
                            )
                            if (
                                isinstance(extension, str)
                                and isinstance(registered_extensions, dict)
                                and extension
                                not in registered_extensions[media_type_file_field]
                            ):
                                message = (
                                    'File "'
                                    + file_check_row[filename_field]
                                    + '" in CSV row "'
                                    + file_check_row[config["id_field"]]
                                    + '" has an extension ('
                                    + str(extension)
                                    + ') that is not allowed in the "'
                                    + media_type_file_field
                                    + '" field of the "'
                                    + media_type
                                    + '" media type.'
                                )
                                logging.error(message)
                                if config["perform_soft_checks"] is False:
                                    sys.exit("Error: " + message)
    # END IF TASK IN CREATE, ADD_MEDIA, UPDATE_MEDIA, UPDATE_MEDIA_BY_NODE AND "file" IN CSV

    # Check existence of fields identified in 'additional_files' config setting.
    if (
        (config["task"] == "create" or config["task"] == "add_media")
        and config["nodes_only"] is False
        and (
            config["paged_content_from_directories"] is False
            or config["paged_content_from_directories_parents_exist"] is False
        )
    ):
        if "additional_files" in config and len(config["additional_files"]) > 0:
            additional_files_entries = get_additional_files_config(config)
            additional_files_check_csv_data = csv_reader.get_csv_data()
            additional_files_fields = additional_files_entries.keys()
            additional_files_fields_csv_headers = csv_reader.get_field_names()
            if config["nodes_only"] is False:
                for additional_file_field in additional_files_fields:
                    if additional_file_field not in additional_files_fields_csv_headers:
                        message = (
                            'CSV column "'
                            + additional_file_field
                            + '" registered in the "additional_files" configuration setting is missing from your CSV file.'
                        )
                        logging.error(message)
                        sys.exit("Error: " + message)

            # Verify media use tids. @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
            if config["nodes_only"] is False:
                for (
                    additional_files_media_use_field,
                    additional_files_media_use_tid,
                ) in additional_files_entries.items():
                    validate_media_use_tid_in_additional_files_setting(
                        config,
                        additional_files_media_use_tid,
                        additional_files_media_use_field,
                    )

            # Check existence of files named in columns identified as 'additional_files' columns.
            missing_additional_files = False
            for count, file_check_row in enumerate(
                additional_files_check_csv_data, start=1
            ):
                for additional_file_field in additional_files_fields:
                    file_check_row[additional_file_field] = file_check_row[
                        additional_file_field
                    ].strip()
                    if len(file_check_row[additional_file_field]) == 0:
                        message = (
                            "CSV row with ID "
                            + file_check_row[config["id_field"]]
                            + ' contains an empty value in its "'
                            + additional_file_field
                            + '" column.'
                        )
                        logging.warning(message)

                    if file_check_row[additional_file_field].startswith("http"):
                        http_response_code = ping_remote_file(
                            config, file_check_row[additional_file_field]
                        )
                        if (
                            http_response_code != 200
                            or ping_remote_file(
                                config, file_check_row[additional_file_field]
                            )
                            is False
                        ):
                            missing_additional_files = True
                            message = (
                                'Additional file "'
                                + file_check_row[additional_file_field]
                                + '" in CSV column "'
                                + additional_file_field
                                + '" in row with ID '
                                + file_check_row[config["id_field"]]
                                + " not found or not accessible (HTTP response code "
                                + str(http_response_code)
                                + ")."
                            )
                            if config["allow_missing_files"] is False:
                                logging.error(message)
                                if config["perform_soft_checks"] is False:
                                    sys.exit("Error: " + message)
                            else:
                                logging.error(message)
                                continue
                    else:
                        if len(file_check_row[additional_file_field]) > 0:
                            if (
                                check_file_exists(
                                    config, file_check_row[additional_file_field]
                                )
                                is False
                            ):
                                missing_additional_files = True
                                message = (
                                    'Additional file "'
                                    + file_check_row[additional_file_field]
                                    + '" in CSV column "'
                                    + additional_file_field
                                    + '" in row with ID '
                                    + file_check_row[config["id_field"]]
                                    + " not found."
                                )
                                if config["allow_missing_files"] is False:
                                    logging.error(message)
                                    if config["perform_soft_checks"] is False:
                                        sys.exit("Error: " + message)
                                else:
                                    logging.error(message)
                                    continue

            if missing_additional_files is True:
                if config["allow_missing_files"] is True:
                    message = '"allow_missing_files" configuration setting is set to "true", and "additional_files" CSV columns containing missing files were detected.'
                    print("Warning: " + message + " See the log for more information.")
                    logging.warning(message + " Details are logged above.")
                else:
                    if config["perform_soft_checks"] is False:
                        sys.exit(message)
            else:
                message = (
                    'OK, files named in "additional_files" CSV columns are all present.'
                )
                print(message)
                logging.info(message)

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        if (
            "additional_files" in config
            and len(config["additional_files"]) > 0
            and config["nodes_only"] is False
        ):
            additional_files_check_extensions_csv_data = csv_reader.get_csv_data()
            # Check media types for files registered in 'additional_files'.
            for count, file_check_row in enumerate(
                additional_files_check_extensions_csv_data, start=1
            ):
                for additional_file_field in additional_files_fields:
                    if len(file_check_row[additional_file_field].strip()) > 0:
                        media_type = set_media_type(
                            config,
                            file_check_row[additional_file_field],
                            additional_file_field,
                            file_check_row,
                        )
                        media_bundle_response_code = ping_media_bundle(
                            config, media_type
                        )
                        if media_bundle_response_code == 404:
                            message = (
                                'File "'
                                + file_check_row[additional_file_field]
                                + '" identified in CSV row '
                                + file_check_row[config["id_field"]]
                                + " will create a media of type ("
                                + media_type
                                + "), but that media type is not configured in the destination Drupal."
                                + " Please make sure your media type configuration matches your Drupal configuration."
                            )
                            logging.error(message)
                            sys.exit("Error: " + message)

                        # Check that each file's extension is allowed for the current media type.
                        additional_filenames = file_check_row[
                            additional_file_field
                        ].split(config["subdelimiter"])
                        media_type_file_field = config["media_type_file_fields"][
                            media_type
                        ]
                        for additional_filename in additional_filenames:
                            if check_file_exists(config, additional_filename):
                                if additional_filename.startswith("http"):
                                    # First check to see if the file has an extension.
                                    extension = os.path.splitext(additional_filename)[1]
                                    if len(extension) > 0:
                                        extension = extension.lstrip(".")
                                        extension = extension.lstrip(".")
                                    else:
                                        extension = get_remote_file_extension(
                                            config, additional_filename
                                        )
                                        extension = extension.lstrip(".")
                                else:
                                    extension = os.path.splitext(additional_filename)
                                    extension = extension[1].lstrip(".").lower()

                                registered_extensions = get_registered_media_extensions(
                                    config, media_type, media_type_file_field
                                )
                                if (
                                    extension
                                    not in registered_extensions[media_type_file_field]
                                ):
                                    message = (
                                        'File "'
                                        + additional_filename
                                        + '" in the "'
                                        + additional_file_field
                                        + '" field of row "'
                                        + file_check_row[config["id_field"]]
                                        + '" has an extension ('
                                        + str(extension)
                                        + ') that is not allowed in the "'
                                        + media_type_file_field
                                        + '" field of the "'
                                        + media_type
                                        + '" media type.'
                                    )
                                    logging.error(message)
                                    sys.exit("Error: " + message)
    # END IF TASK == CREATE or ADD_MEDIA and not nodes_only and not paged_content_from_directories

    # @todo Add warning to accommodate #639
    if config["task"] == "create" and (
        config["paged_content_from_directories"] is True
        or config["paged_content_from_directories_parents_exist"] is True
    ):
        if "paged_content_page_model_tid" not in config:
            message = 'If you are creating paged content, you must include "paged_content_page_model_tid" in your configuration.'
            logging.error(
                'Configuration requires "paged_content_page_model_tid" setting when creating paged content.'
            )
            sys.exit("Error: " + message)

        if config["paged_content_from_directories_parents_exist"] is True:
            if "field_member_of" not in csv_column_headers:
                message = '"field_member_of" is a required column in your input CSV when using the "paged_content_from_directories_parents_exist: true" configuration setting.'
                logging.error(message)
                sys.exit("Error: " + message)

        if "paged_content_additional_page_media" in config:
            disable_action_message = (
                'Including the "paged_content_additional_page_media" setting in your configuration will create '
                + "media that are normally generated by Islandora microservices. You should disable any actions your Drupal Contexts "
                + '"Derivatives" configuration so that Islandora does not also generate duplicate media.'
            )
            logging.warning(disable_action_message)
            print("Warning: " + disable_action_message)

            if (
                "paged_content_image_file_extension" not in config
                or "paged_content_additional_page_media" not in config
            ):
                message = (
                    'If your configuration contains the "paged_content_additional_page_media" setting, it must also include both '
                    + 'the "paged_content_image_file_extension" and "paged_content_additional_page_media" settings.'
                )
                logging.error(message)
                sys.exit("Error: " + message)

        paged_content_sequence_indicator_warnings = False
        paged_content_from_directories_csv_data = csv_reader.get_csv_data()
        for count, file_check_row in enumerate(
            paged_content_from_directories_csv_data, start=1
        ):
            dir_path = os.path.join(
                config["input_dir"],
                file_check_row[config["page_files_source_dir_field"]],
            )
            if not os.path.exists(dir_path) or os.path.isfile(dir_path):
                message = (
                    "Page directory "
                    + dir_path
                    + ' for CSV record with ID "'
                    + file_check_row[config["id_field"]]
                    + '"" not found.'
                )
                logging.error(message)
                sys.exit("Error: " + message)
            page_files = os.listdir(dir_path)
            if len(page_files) == 0:
                message = "Page directory " + dir_path + " is empty."
                print("Warning: " + message)
                logging.warning(message)

            for page_file_name in page_files:
                # Only want files, not directories.
                if os.path.isdir(os.path.join(dir_path, page_file_name)):
                    continue

                if paged_content_ignore_file(config, page_file_name) is True:
                    logging.info(
                        f'Ignoring file "{os.path.join(dir_path, page_file_name)}" since it matches an entry in the "paged_content_ignore_files" config setting.'
                    )
                    continue

                if paged_content_ignore_file(config, page_file_name) is False:
                    if config["paged_content_sequence_separator"] not in page_file_name:
                        message = (
                            "Page file "
                            + os.path.join(dir_path, page_file_name)
                            + " does not contain a sequence separator ("
                            + config["paged_content_sequence_separator"]
                            + ")."
                        )
                        logging.warning(message)
                        paged_content_sequence_indicator_warnings = True

                page_sequence_indicator = get_sequence_indicator_from_filename(
                    config, page_file_name
                )
                if validate_weight_value(page_sequence_indicator) is False:
                    if paged_content_ignore_file(config, page_file_name) is False:
                        logging.warning(
                            f'Sequence indicator in page filename "{os.path.join(dir_path, page_file_name)}" is not a valid "field_weight" value.'
                        )
                        paged_content_sequence_indicator_warnings = True

            # Check additional page media files (e.g. OCR andhOCR files) for utf8 encoding.
            additional_page_media_no_utf8_warnings = list()
            if (
                config["paged_content_from_directories"] is True
                or config["paged_content_from_directories_parents_exist"] is True
            ):
                if "paged_content_additional_page_media" in config:
                    for extension_mapping in config[
                        "paged_content_additional_page_media"
                    ]:
                        for (
                            additional_page_media_use_term,
                            additional_page_media_extension,
                        ) in extension_mapping.items():
                            for page_file_name in page_files:
                                page_file_base_path, page_file_extension = (
                                    os.path.splitext(page_file_name)
                                )
                                if (
                                    page_file_extension.lstrip(".")
                                    == additional_page_media_extension
                                ):
                                    additional_page_media_file_path = os.path.join(
                                        dir_path,
                                        page_file_base_path
                                        + "."
                                        + additional_page_media_extension.strip(),
                                    )
                                    if check_file_exists(
                                        config, additional_page_media_file_path
                                    ):
                                        if (
                                            file_is_utf8(
                                                additional_page_media_file_path
                                            )
                                            is False
                                        ):
                                            message = (
                                                'Additional page/child media file "'
                                                + additional_page_media_file_path
                                                + '" in directory for row ID "'
                                                + row[config["id_field"]]
                                                + '" is not encoded as UTF-8 so will not be ingested.'
                                            )
                                            if (
                                                additional_page_media_file_path
                                                not in additional_page_media_no_utf8_warnings
                                            ):
                                                logging.warning(message)
                                                additional_page_media_no_utf8_warnings.append(
                                                    additional_page_media_file_path
                                                )

        print("OK, page directories are all present.")
        if paged_content_sequence_indicator_warnings is True:
            print(
                "Warning: Check your Workbench log for entries about sequence indicator/field_weight values for page/child files."
            )
        if len(additional_page_media_no_utf8_warnings) > 0:
            print(
                "Warning: Check your Workbench log for entries about UTF-8 encoding of additional page/child files."
            )
    # END IF TASK == CREATE and paged_content_from_directories

    # Check for bootstrap scripts, if any are configured.
    if "bootstrap" in config and len(config["bootstrap"]) > 0:
        for bootstrap_script in config["bootstrap"]:
            if not os.path.exists(bootstrap_script):
                message = "Bootstrap script " + bootstrap_script + " not found."
                logging.error(message)
                sys.exit("Error: " + message)
            if os.access(bootstrap_script, os.X_OK) is False:
                message = "Bootstrap script " + bootstrap_script + " is not executable."
                logging.error(message)
                sys.exit("Error: " + message)

            message = "OK, registered bootstrap scripts found and executable."
            logging.info(message)
            print(message)

    # Check for shutdown scripts, if any are configured.
    if "shutdown" in config and len(config["shutdown"]) > 0:
        for shutdown_script in config["shutdown"]:
            if not os.path.exists(shutdown_script):
                message = "shutdown script " + shutdown_script + " not found."
                logging.error(message)
                sys.exit("Error: " + message)
            if os.access(shutdown_script, os.X_OK) is False:
                message = "Shutdown script " + shutdown_script + " is not executable."
                logging.error(message)
                sys.exit("Error: " + message)

        message = "OK, registered shutdown scripts found and executable."
        logging.info(message)
        print(message)

    # Check for preprocessor scripts, if any are configured.
    if "preprocessors" in config and len(config["preprocessors"]) > 0:
        # for preprocessor_script in config['preprocessors']:
        for field, script_path in config["preprocessors"].items():
            if not os.path.exists(script_path):
                message = f'Preprocessor script "{script_path}" for field "{field}" not found.'
                logging.error(message)
                sys.exit("Error: " + message)
            if os.access(script_path, os.X_OK) is False:
                message = f'Preprocessor script "{script_path}" for field "{field}" is not executable.'
                logging.error(message)
                sys.exit("Error: " + message)

        message = f"OK, registered preprocessor scripts found and executable."
        logging.info(message)
        print(message)

    # Check for the existence and executableness of post-action scripts, if any are configured.
    if (
        config["task"] == "create"
        or config["task"] == "update"
        or config["task"] == "add_media"
    ):
        post_action_scripts_configs = [
            "node_post_create",
            "node_post_update",
            "media_post_create",
        ]
        for post_action_script_config in post_action_scripts_configs:
            post_action_scripts_present = False
            if (
                post_action_script_config in config
                and len(config[post_action_script_config]) > 0
            ):
                post_action_scripts_present = True
                for post_action_script in config[post_action_script_config]:
                    if not os.path.exists(post_action_script):
                        message = (
                            "Post-action script " + post_action_script + " not found."
                        )
                        logging.error(message)
                        sys.exit("Error: " + message)
                    if os.access(post_action_script, os.X_OK) is False:
                        message = (
                            "Post-action script "
                            + post_action_script
                            + " is not executable."
                        )
                        logging.error(message)
                        sys.exit("Error: " + message)
            if post_action_scripts_present is True:
                message = "OK, registered post-action scripts found and executable."
                logging.info(message)
                print(message)

    if config["task"] == "export_csv":
        if "node_id" not in csv_column_headers:
            message = (
                'For "export_csv" tasks, your CSV file must contain a "node_id" column.'
            )
            logging.error(message)
            sys.exit("Error: " + message)

        export_csv_term_mode_options = ["tid", "name"]
        if config["export_csv_term_mode"] not in export_csv_term_mode_options:
            message = 'Configuration option "export_csv_term_mode_options" must be either "tid" or "name".'
            logging.error(message)
            sys.exit("Error: " + message)

        if config["export_file_directory"] is not None:
            if not os.path.exists(config["export_csv_file_path"]):
                try:
                    os.mkdir(config["export_file_directory"])
                    os.rmdir(config["export_file_directory"])
                except Exception as e:
                    message = (
                        'Path in configuration option "export_file_directory" ("'
                        + config["export_file_directory"]
                        + '") is not writable.'
                    )
                    logging.error(message + " " + str(e))
                    sys.exit("Error: " + message + " See log for more detail.")

        if config["export_file_media_use_term_id"] is False:
            message = f'Unknown value for configuration setting "export_file_media_use_term_id": {config["export_file_media_use_term_id"]}.'
            logging.error(message)
            sys.exit("Error: " + message)
    ### END IF TASK == EXPORT_CSV

    # Checks for "run_scripts" task.
    if config["task"] == "run_scripts":
        run_scripts_check_csv_data = csv_reader.get_csv_data()
        csv_column_headers = csv_reader.get_field_names()
        if "run_scripts_entity_type" not in config:
            message = 'Required "run_scripts_entity_type" setting not in config file.'
            logging.error(message)
            sys.exit("Error: " + message)
        if config["run_scripts_entity_type"] not in ["node", "media", "term"]:
            message = 'Required "run_scripts_entity_type" setting must be on of "node", "media" or "term".'
            logging.error(message)
            sys.exit("Error: " + message)

        if (
            config["run_scripts_entity_type"] == "node"
            and "node_id" not in csv_column_headers
        ):
            message = 'run_scripts tasks for nodes require a "node_id" column in the intput CSV.'
            logging.error(message)
            sys.exit("Error: " + message)
        if (
            config["run_scripts_entity_type"] == "media"
            and "media_id" not in csv_column_headers
        ):
            message = 'run_scripts tasks for media require a "media_id" column in the intput CSV.'
            logging.error(message)
            sys.exit("Error: " + message)
        if (
            config["run_scripts_entity_type"] == "term"
            and "term_id" not in csv_column_headers
        ):
            message = 'run_scripts tasks for taxonomy terms a require a "term_id" column in the intput CSV.'
            logging.error(message)
            sys.exit("Error: " + message)

        if "run_scripts" not in config:
            message = 'Required "run_scripts" setting not in config file.'
            logging.error(message)
            sys.exit("Error: " + message)
        if "run_scripts" in config and len(config["run_scripts"]) == 0:
            message = 'Required "run_scripts" setting is in config file but does contain any scripts.'
            logging.error(message)
            sys.exit("Error: " + message)

        for script_to_run in config["run_scripts"]:
            if " " in script_to_run:
                interpeter, script_to_run = script_to_run.split(" ", 1)
            if not os.path.exists(script_to_run.strip()):
                message = "Script " + script_to_run + " not found."
                logging.error(message)
                sys.exit("Error: " + message)
            if os.access(script_to_run, os.X_OK) is False:
                message = "Script " + script_to_run + " is not executable."
                logging.error(message)
                sys.exit("Error: " + message)

        message = "OK, registered scripts to run found and executable."
        logging.info(message)
        print(message)

        # Ping each entity.
        entities_found = True
        if config["run_scripts_entity_type"] == "node":
            for row in run_scripts_check_csv_data:
                if ping_node(config, row["node_id"], warn=False) is False:
                    logging.warning(f'Node ID {row["node_id"]} not found.')
                    entities_found = False
        if config["run_scripts_entity_type"] == "media":
            for row in run_scripts_check_csv_data:
                if ping_media(config, row["media_id"], warn=False) is False:
                    logging.warning(f'Media ID {row["media_id"]} not found.')
                    entities_found = False
        if config["run_scripts_entity_type"] == "term":
            for row in run_scripts_check_csv_data:
                if ping_term(config, row["term_id"]) is False:
                    logging.warning(f'Term ID {row["term_id"]} not found.')
                    entities_found = False

        if entities_found == True:
            logging.info("All entities listed in input CSV found.")
            print("OK, all entities listed in input CSV found.")
        else:
            message = "Some entities listed in input CSV not found; please see your Workbench log for more detail."
            print("Warning: " + message)
    ### END IF TASK == RUN_SCRIPTS

    # If nothing has failed by now, exit with a positive, upbeat message.
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
        'Configuration checked for "%s" task using config file "%s", no problems found.',
        config["task"],
        args.config,
    )

    if "check_lock_file_path" in config:
        with open(config["check_lock_file_path"], "a") as check_lock_file:
            config_file_md5 = get_file_hash_from_local(
                config, config["config_file_path"], "md5"
            )
            check_lock_file.write(
                f'Check against {config["config_file_path"]} (md5 hash {config_file_md5}) OK'
            )
            logging.info(
                f"Writing --check lock file \"{config['check_lock_file_path']}\"."
            )

    if args.contactsheet is True:
        if os.path.isabs(config["contact_sheet_output_dir"]):
            contact_sheet_path = os.path.join(
                config["contact_sheet_output_dir"], "contact_sheet.htm"
            )
        else:
            contact_sheet_path = os.path.join(
                os.getcwd(), config["contact_sheet_output_dir"], "contact_sheet.htm"
            )
        generate_contact_sheet_from_csv(config)
        message = f"Contact sheet is at {contact_sheet_path}."
        print(message)
        logging.info(message)

    if config["secondary_tasks"] is None:
        sys.exit(0)
    else:
        for secondary_config_file in json.loads(
            os.environ["ISLANDORA_WORKBENCH_SECONDARY_TASKS"]
        ):
            print("")
            print(
                'Running --check using secondary configuration file "'
                + secondary_config_file
                + '"'
            )
            if os.name == "nt":
                # Assumes python.exe is in the user's PATH.
                cmd = [
                    "python",
                    "./workbench",
                    "--config",
                    secondary_config_file,
                    "--check",
                ]
            else:
                cmd = ["./workbench", "--config", secondary_config_file, "--check"]
            output = subprocess.run(cmd)

        sys.exit(0)
