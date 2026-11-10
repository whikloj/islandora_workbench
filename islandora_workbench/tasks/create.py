import copy
import datetime
import json
import os
from typing import Union

from progress_bar import InitBar

from islandora_workbench.checks.validation import validate_url_aliases_action, validate_node_created_date_action, \
    validate_node_uid_action, validate_geolocation_fields_action, validate_link_fields_action, \
    validate_authority_link_value_action, validate_taxonomy_reference_value_action, \
    validate_typed_relation_field_value_action, validate_numeric_field_action, validate_weight_value, \
    validate_media_track_value, validate_required_fields_have_values_action, validate_input_dir, \
    validate_entity_reference_view_endpoints, validate_media_use_tid, validate_media_use_tids_in_csv_action, \
    validate_media_use_tid_in_additional_files_setting
from islandora_workbench.data_io.csv.csv_process import apply_csv_value_templates, preprocessor_scripts_csv_row
from islandora_workbench.data_io.output_csv import OutputCSVWriter
from islandora_workbench.data_io.rollback import RollbackMgr
from islandora_workbench.drupal.fields import WorkbenchFieldFactory, get_required_bundle_fields, get_field_definitions
from islandora_workbench.tasks import register_task
from islandora_workbench.tasks.base import Task
from islandora_workbench.exceptions import WorkbenchValidationException, WorkbenchValidationWarning, WorkbenchException
from workbench_utils import is_running_in_docker, get_additional_files_config, ping_remote_file, paged_content_ignore_file, get_page_title_from_template
from islandora_workbench.checks.internal import check_rollback_file_path_directories
from islandora_workbench.external_scripts import execute_entity_post_task_script
from islandora_workbench.drupal.client import ping_node, get_term_id_from_uri, get_all_representations_of_term, \
    create_node, create_url_alias, create_media, get_entity_fields, ping_view_endpoint, \
    ping_entity_reference_view_endpoint, verify_node_exists_by_key
from islandora_workbench.drupal.utils import validate_language_code, get_nid_from_url_alias
from islandora_workbench.utils import value_is_numeric, file_is_utf8, get_sequence_indicator_from_filename, \
    truncate_csv_value, get_file_hash_from_local, check_file_exists


@register_task("create")
class CreateTask(Task):

    name = "create"
    required_config_keys = ("task", "host", "username", "password")

    def __init__(self, ctx: dict):
        super().__init__(ctx)
        temp_required_fields = get_required_bundle_fields(self.ctx["config"], "node", self.ctx["config"]["content_type"])
        temp_required_fields.extend([self.id_field, "title"])
        if (
                self.ctx["config"]["nodes_only"] is False
                and self.ctx["config"]["paged_content_from_directories"] is False
                and self.ctx["config"]["paged_content_from_directories_parents_exist"] is False
        ):
            temp_required_fields.append("file")
        self.required_fields = tuple(temp_required_fields)

        self.field_configs["entity_fields"] = list(
            get_entity_fields(self.ctx['config'], "node", self.ctx['config']["content_type"]).keys()
        )
        self.field_configs["entity_field_definitions"] = get_field_definitions(self.ctx["config"], "node")
        if self.ctx['config']["id_field"] not in self.field_configs["entity_fields"]:
            self.field_configs["required_fields"].append(self.ctx['config']["id_field"])

        if "additional_files" in self.ctx["config"] and len(self.ctx["config"]["additional_files"]) > 0:
            self.field_configs["additional_files"] = self.ctx["config"]["additional_files"]
        else:
            self.field_configs["additional_files"] = {}
        self.field_configs["required_bundle_fields"] = get_required_bundle_fields(
            self.ctx["config"], "node", self.ctx["config"]["content_type"]
            )

        if "output_csv" in self.config_keys and not self.is_check_mode:
            # Add a OutputCSVWriter instance to the context so we can write to the output CSV file from process_row().
            fields_to_exclude = copy.copy(self.RESERVED_FIELDS)
            fields_to_exclude.extend(self.field_configs["additional_files"].keys())
            self.ctx["output_csv_writer"] = OutputCSVWriter(self.ctx, self.id_field, fields_to_exclude)

    def general_checks(self):
        """Perform general checks for a create task."""
        validate_input_dir(self.ctx["config"])

        if len(self.field_configs["entity_fields"]) == 0:
            message = "Workbench cannot retrieve field definitions from Drupal. Please confirm that the Field, Field Storage, and Entity Form Display REST resources are enabled."
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

        if self.ctx["config"]["csv_id_to_node_id_map_dir"] == self.ctx["config"]["temp_dir"]:
            message = f'You should set your "csv_id_to_node_id_map_dir" config setting to a location other than your system\'s temporary directory ("{self.ctx["config"]["temp_dir"]}").'
            self.ctx["log"].warning(message)

        if is_running_in_docker():
            docker_message = (
                "It appears you are running Workbench within a Docker container. Please ensure that your CSV ID to node ID map "
                + "config setting defines a location accessible outside of the Docker container; otherwise, it might be deleted when you destroy or rebuild your Docker image."
            )
            self.ctx["log"].warning(docker_message)

        if (
            self.ctx["config"]["recovery_mode_starting_from_node_id"] is not False
            and value_is_numeric(self.ctx["config"]["recovery_mode_starting_from_node_id"]) is True
        ):
            message = f'"recovery_mode" option in effect. Items that have already been ingested with node IDs starting at {self.ctx["config"]["recovery_mode_starting_from_node_id"]} will be skipped.'
            self.ctx["log"].info(message)

        # Check for presence of required config keys, which varies by task.
        if self.ctx["config"]["nodes_only"] is True:
            self.ctx["log"].info('"nodes_only" option in effect. Media files will not be checked/validated.')

        self.ctx["log"].info("OK, configuration file has all required values (did not check for optional values).")

        check_rollback_file_path_directories(self.ctx["config"])

        # Perform general checks on the CSV file for a create task.

        # This is the set of conditions where the map is queried to get parent node IDs. AFAIK it's
        # complete but if others come up, they should be added here.
        check_for_parent_csv_headers = self.ctx["csv"].field_names
        if (
            len(self.ctx["config"]["csv_id_to_node_id_map_allowed_hosts"]) > 0
            or (os.environ.get("ISLANDORA_WORKBENCH_SECONDARY_TASKS") is not None)
            or (
                "parent_id" in check_for_parent_csv_headers
                and self.ctx["config"]["query_csv_id_to_node_id_map_for_parents"] is True
            )
            or (
                self.ctx["config"]["recovery_mode_starting_from_node_id"] is not False
                and value_is_numeric(self.ctx["config"]["recovery_mode_starting_from_node_id"])
                is True
            )
        ):
            csv_to_node_id_map_path = self.ctx["config"]["csv_id_to_node_id_map_path"]
            current_host = self.ctx["config"]["host"]

            with self.ctx["node_map"].get_all_hosts() as unique_hosts:
                if len(unique_hosts) > 0:
                    unique_host_values = list(unique_hosts)
                    # TODO: should this be removing any blank value or just the first?
                    if "" in unique_host_values:
                        unique_host_values.remove("")
                    if current_host in unique_host_values:
                        unique_host_values.remove(current_host)
                    list_of_hosts = ", ".join(unique_host_values).strip()
                    if len(unique_host_values) > 0:
                        multiple_hosts_in_map_log_message = (
                            'There are values for the "host" column in the CSV ID to node ID map '
                            + f'at "{csv_to_node_id_map_path}" other than "" (empty) and your currently configured host ("{current_host}"). '
                            + f"Those extra hosts are {list_of_hosts}. Please see https://mjordan.github.io/islandora_workbench_docs/csv_id_to_node_id_map/"
                            + " for advice on what to do."
                        )
                        multiple_hosts_in_map_console_message = (
                            'There are values for the "host" column in the CSV ID to node ID map '
                            + f'at "{csv_to_node_id_map_path}" other than your current "host" configuration setting. Please see your workbench log for more information.'
                        )
                        self.ctx["log"].warning(multiple_hosts_in_map_console_message, log_message=multiple_hosts_in_map_log_message)
                    else:
                        self.ctx["log"].info(
                            'No unexpected values in the CSV ID to node ID map\'s "host" column.'
                        )

        csv_column_headers = self.ctx["csv"].field_names

        if "output_csv" in self.config_keys:
            if os.path.exists(self.ctx["config"]["output_csv"]):
                message = ("Output CSV already exists at " + self.ctx["config"][
                    "output_csv"] + ", records will be appended to it.")
                self.ctx["log"].info(message)

        # Assumes that only primary tasks have something in their 'secondary_tasks' config setting.
        if self.ctx["config"]["secondary_tasks"] is not None:
            # We populate the ISLANDORA_WORKBENCH_PRIMARY_TASK_EXECUTION_START_TIME environment variable here so secondary
            # tasks can access it during in validate_parent_ids_in_csv_id_to_node_id_map().
            workbench_execution_start_time = "{:%Y-%m-%d %H:%M:%S}".format(
                datetime.datetime.now()
            )
            os.environ["ISLANDORA_WORKBENCH_PRIMARY_TASK_EXECUTION_START_TIME"] = (
                workbench_execution_start_time)

        # Specific to creating aggregated content such as collections, compound objects and paged content. Currently, if 'parent_id' is present
        # in the CSV file 'field_member_of' is mandatory.
        if "parent_id" in csv_column_headers:
            if "field_weight" not in csv_column_headers:
                self.ctx["log"].info(
                    'If you are ingesting compound objects, a "field_weight" column is required in your input CSV file.'
                    )
            if "field_member_of" not in csv_column_headers:
                message = 'If your CSV file contains a "parent_id" column, it must also contain a "field_member_of" column (with empty values in child rows).'
                self.ctx["log"].error(message)
                raise WorkbenchValidationException(message)

        all_fields_check_list = []
        all_fields_check_list.extend(self.field_configs["entity_fields"])
        all_fields_check_list.extend(self.NODE_BASE_FIELDS)
        all_fields_check_list.extend(self.field_configs["reserved_fields"])
        all_fields_check_list.extend(self.field_configs["additional_files"].keys())
        all_fields_check_list.append(self.ctx["config"]["id_field"])
        if "ignore_csv_columns" in self.ctx["config"].keys():
            all_fields_check_list.extend(self.ctx["config"]["ignore_csv_columns"])
        # Check for any column headers that do not exist in the above lists or is the configured "id_field"
        missing_drupal_fields = [header for header in csv_column_headers if header not in all_fields_check_list]

        if len(missing_drupal_fields) > 0:
            missing_drupal_fields_message = ", ".join(missing_drupal_fields)
            self.ctx["log"].error(
                f"The following header(s) require a matching Drupal field name: {missing_drupal_fields_message}."
            )
            raise WorkbenchValidationException(missing_drupal_fields_message)

        # We .remove() CSV column headers for this check because they are not Drupal field names (including 'langcode').
        csv_column_headers = [header for header in csv_column_headers if
                              header not in self.field_configs["reserved_fields"]]

        # langcode is a standard Drupal field but it doesn't show up in any field configs.
        # Set this so we can validate langcode below in check_row.
        self.testing_flags["langcode_was_present"] = "langcode" in csv_column_headers
        if "langcode" in csv_column_headers:
            csv_column_headers.remove("langcode")

        # We .remove() CSV column headers that use the 'media:video:field_foo' media track convention.
        media_track_headers = list()
        for column_header in csv_column_headers:
            if column_header.startswith("media:"):
                media_track_header_parts = column_header.split(":")
                # We also validate the structure of the media track column headers.
                if len(media_track_header_parts) != 3:
                    message = f'"{column_header}" is not a valid media track CSV header.'
                    self.ctx["log"].error(message)
                    raise WorkbenchValidationException(message)
                if (media_track_header_parts[1] in self.ctx["config"]["media_track_file_fields"].keys() and
                        media_track_header_parts[2] == self.ctx["config"]["media_track_file_fields"][
                            media_track_header_parts[1]]):
                    media_track_headers.append(column_header)
        csv_column_headers = [header for header in csv_column_headers if header not in media_track_headers]

        # Check the configuration that is necessary for verifying nodes already exist in the target Drupal.
        if "node_exists_verification_view_endpoint" in self.ctx["config"]:
            node_exists_config = self.ctx["config"]["node_exists_verification_view_endpoint"]
            if node_exists_config is not False:
                if node_exists_config[0] not in csv_column_headers:
                    message = f'CSV column identified in "node_exists_verification_view_endpoint" is not in your CSV file.'
                    self.ctx["log"].error(message)
                    raise WorkbenchValidationException(message)
                view_url = f'{self.ctx["config"]["host"]}/{node_exists_config[1].lstrip("/")}'
                view_path_status_code = ping_view_endpoint(self.ctx["config"], view_url)
                if view_path_status_code != 200:
                    message = f'Cannot access View REST export configured in "node_exists_verification_view_endpoint" ({view_url}).'
                    self.ctx["log"].error(message)
                    raise WorkbenchValidationException(message)
                else:
                    message = f'View REST export configured in "node_exists_verification_view_endpoint" ({view_url}) is accessible. Values in the "{node_exists_config[0]}" CSV column will be used to check whether nodes already exist.'
                    self.ctx["log"].info(message)

        # Check for the View that is necessary for entity reference fields configured
        # as "Views: Filter by an entity reference View" (issue 452).
        for csv_column_header in csv_column_headers:
            if csv_column_header in self.field_configs["entity_field_definitions"] and \
                    self.field_configs["entity_field_definitions"][csv_column_header]["handler"] == "views":
                if (self.ctx["config"][
                    "require_entity_reference_views"] is True and "entity_reference_view_endpoints" not in self.ctx[
                    "config"]):
                    entity_reference_view_exists = ping_entity_reference_view_endpoint(
                        self.ctx["config"], csv_column_header, self.field_configs["entity_field_definitions"][csv_column_header]["handler_settings"], )
                    if not entity_reference_view_exists:
                        console_message = ('Workbench cannot access the View "' +
                                           self.field_configs["entity_field_definitions"][csv_column_header]["handler_settings"]["view"][
                                               "view_name"] + '" required to validate values for field "' + csv_column_header + '". See log for more detail.')
                        log_message = ('Workbench cannot access the path defined by the REST Export display "' +
                                       self.field_configs["entity_field_definitions"][csv_column_header]["handler_settings"]["view"][
                                           "display_name"] + '" in the View "' +
                                       self.field_configs["entity_field_definitions"][csv_column_header]["handler_settings"]["view"][
                                           "view_name"] + '" required to validate values for field "' + csv_column_header + '". Please check your Drupal Views configuration.' + ' See the "Entity Reference Views fields" section of ' + "https://mjordan.github.io/islandora_workbench_docs/fields/#entity-reference-views-fields for more info.")
                        self.ctx["log"].error(console_message, log_message=log_message)
                        raise WorkbenchValidationException(console_message)
                else:
                    message = f'Workbench will not validate values in your CSV file\'s "{csv_column_header}" column because your "require_entity_reference_views" configuration setting is "false".'
                    self.ctx["log"].warning(
                        message,
                        log_message=message + ' See the "Entity Reference Views fields" section of ' + "https://mjordan.github.io/islandora_workbench_docs/fields/#entity-reference-views-fields for more info."
                        )

        self.ctx["log"].info("OK, CSV column headers match Drupal field names.")

        if "field_viewer_override_extensions" in self.ctx["config"] or "field_viewer_override_models" in self.ctx["config"]:
            preprocessed_input_csv_file_path = self.ctx["csv"].path
            message = f'You should review "{preprocessed_input_csv_file_path}" to ensure that values in the "field_viewer_override" column have been correctly assigned based on your configuration settings.'
            self.ctx["log"].warning(message)

        missing_required_fields = [k for k in self.field_configs["required_bundle_fields"] if k not in csv_column_headers]
        if ("field_model" in missing_required_fields and self.ctx["config"][
            "paged_content_from_directories_parents_exist"] is True and self.ctx["config"].get(
            "paged_content_page_model_tid"
            ) is not None):
            # If we are ingesting paged content from directories and the parent nodes already exist, we don't need to require the field_model field in the CSV file.
            missing_required_fields.remove("field_model")
        if len(missing_required_fields) > 0:
            message = f"The following required Drupal fields for content type {self.ctx['config']['content_type']} are not present in the CSV file: {', '.join(missing_required_fields)}."
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

        self.ctx["log"].info("OK, required Drupal fields are present in the CSV file.")

        if not self.ctx["config"]["nodes_only"]:
            validate_media_use_tid(self.ctx["config"])
            if (
                    "fixity_algorithm" in self.ctx["config"]
                    and self.ctx["config"]["fixity_algorithm"] is not None
                    and self.ctx["config"]["fixity_algorithm"] not in ["md5", "sha1", "sha256"]
                ):
                    message = ("Configured fixity algorithm '" + self.ctx["config"][
                        "fixity_algorithm"] + "' must be one of 'md5', 'sha1', or 'sha256'.")
                    self.ctx["log"].error(message)
                    raise WorkbenchValidationException(message)

        self.generate_field_lists(csv_column_headers)

        # Validate media bundles (after we generated the list of media fields)
        for field_name, bundle_name in self.field_types["media_fields"].items():
            if bundle_name not in self.ctx["config"]["media_track_file_fields"]:
                message = (
                        f'Media type "{bundle_name}" in the CSV column header "{field_name}" is not registered in the '
                        f'"media_track_file_fields" configuration setting.'
                )
                raise WorkbenchValidationException(message)
            if bundle_name not in self.field_configs["media_bundle_field_definitions"]:
                # It's a valid bundle so store the media bundle definition for later per-row field validation.
                self.field_configs["media_bundle_field_definitions"][bundle_name] = get_field_definitions(
                self.ctx["config"], "media", bundle_name
                )

        # Check the configuration that is necessary for enabling use of term names in Entity Reference Views fields.
        validate_entity_reference_view_endpoints(self.ctx, csv_column_headers)

        if not self.ctx["config"]["nodes_only"] and not (self.ctx["config"]["paged_content_from_directories"] or self.ctx["config"]["paged_content_from_directories_parents_exist"]):
            if "additional_files" in self.config_keys and len(self.ctx["config"]["additional_files"]) > 0:
                for additional_file_field, media_use_tid in self.field_configs["additional_files"].items():
                    if additional_file_field not in csv_column_headers:
                        message = (
                                'CSV column "' + additional_file_field + '" registered in the "additional_files" configuration setting is missing from your CSV file.')
                        self.ctx["log"].error(message)
                        raise WorkbenchValidationException(message)
                    try:
                        validate_media_use_tid_in_additional_files_setting(self.ctx["config"], media_use_tid, additional_file_field)
                    except WorkbenchValidationWarning as e:
                        self.ctx["log"].warning(str(e))
        if self.ctx["config"]["paged_content_from_directories"] or self.ctx["config"]["paged_content_from_directories_parents_exist"]:
            if "paged_content_page_model_tid" not in self.config_keys:
                self.ctx["log"].error('Configuration requires "paged_content_page_model_tid" setting when creating paged content.')
                raise WorkbenchValidationException('Configuration requires "paged_content_page_model_tid" setting when creating paged content.')

            if self.ctx["config"]["paged_content_from_directories_parents_exist"]:
                if "field_member_of" not in csv_column_headers:
                    message = '"field_member_of" is a required column in your input CSV when using the "paged_content_from_directories_parents_exist: true" configuration setting.'
                    self.ctx["log"].error(message)
                    raise WorkbenchValidationException(message)

            if "paged_content_additional_page_media" in self.config_keys:
                disable_action_message = (
                    'Including the "paged_content_additional_page_media" setting in your configuration will create '
                    + "media that are normally generated by Islandora microservices. You should disable any actions your Drupal Contexts "
                    + '"Derivatives" configuration so that Islandora does not also generate duplicate media.'
                )
                self.ctx["log"].warning(disable_action_message)

            if "paged_content_image_file_extension" not in self.config_keys or "paged_content_additional_page_media" not in self.config_keys:
                message = (
                    'If your configuration contains the "paged_content_additional_page_media" setting, it must also include both '
                    + 'the "paged_content_image_file_extension" and "paged_content_additional_page_media" settings.'
                )
                self.ctx["log"].error(message)
                raise WorkbenchValidationException(message)


    def check_row(self, row: Union[dict, str], row_number: int):
        """Check the current row for any issues."""
        csv_column_headers = copy.copy(self.ctx["csv"].field_names)
        row_id = row[self.id_field]
        row_level_errors = []
        row_level_warnings = []

        if "url_alias" in csv_column_headers:
            try:
                validate_url_aliases_action(self.ctx["config"], row)
            except WorkbenchValidationException as e:
                self.ctx["log"].error(str(e), log_only=True)
                row_level_errors.append(str(e))

        try:
            validate_required_fields_have_values_action(
                self.ctx["config"], self.field_configs["required_bundle_fields"], row
            )
        except WorkbenchValidationException as e:
            self.ctx["log"].error(str(e), log_only=True)
            row_level_errors.append(str(e))

        try:
            # Validate dates in 'created' field, if present.
            validate_node_created_date_action(self.ctx["config"], row)
        except WorkbenchValidationException as e:
            self.ctx["log"].error(str(e), log_only=True)
            row_level_errors.append(str(e))

        try:
            # Validate user IDs in 'uid' field, if present.
            validate_node_uid_action(self.ctx["config"], row)
        except WorkbenchValidationException as e:
            self.ctx["log"].error(str(e), log_only=True)
            row_level_errors.append(str(e))

        if not self.ctx["config"]["nodes_only"]:
            try:
                validate_media_use_tids_in_csv_action(self.ctx["config"], row)
            except WorkbenchValidationException as e:
                self.ctx["log"].error(str(e), log_only=True)
                row_level_errors.append(str(e))

            if not (self.ctx["config"]["paged_content_from_directories"] or self.ctx["config"]["paged_content_from_directories_parents_exist"]):
                # validate files exist
                if "file" in row:
                    file_name = row["file"].strip()
                    # Check for and log empty 'file' values.
                    if len(file_name) == 0:
                        message = ("CSV row with ID " + row[
                            self.ctx["config"]["id_field"]] + ' contains an empty "file" value.')
                        self.ctx["log"].warning(message)
                    if not check_file_exists(self.ctx["config"], file_name):
                        prefix = "Remote file" if file_name.lower().startswith("http") else "Local file"
                        message = (f'{prefix} "{file_name}" identified in CSV "file" column for row with ID "' +
                                   row_id + '" not found.')
                        if self.ctx["config"]["allow_missing_files"] is False:
                            self.ctx["log"].error(message)
                            if not self.ctx["config"]["perform_soft_checks"]:
                                row_level_errors.append(message)
                    elif "checksum" in row and file_name.lower().startswith("http"):
                        # If the file is local and does exist, we can check the checksum
                        hash_from_local = get_file_hash_from_local(file_name, self.ctx["config"]["fixity_algorithm"])
                        if hash_from_local != False:
                            if hash_from_local == row["checksum"].strip():
                                self.ctx["log"].info(
                                    f'Local {self.ctx["config"]["fixity_algorithm"]} checksum and value in the CSV "checksum" field for file "{file_name}" ({hash_from_local}) match.',
                                    log_only=True
                                )
                            else:
                                self.ctx["log"].warning(
                                    f'Local {self.ctx["config"]["fixity_algorithm"]} checksum and value in the CSV "checksum" field for file "{file_name}" (named in CSV row "{row_id}") do not match (local: {hash_from_local}, CSV: {row["checksum"]}).',
                                )
                        else:
                            self.ctx["log"].warning(
                                f'Could not calculate local {self.ctx["config"]["fixity_algorithm"]} checksum for file "{file_name}".',
                            )

                if "additional_files" in self.config_keys and len(self.ctx["config"]["additional_files"]) > 0:
                    # Check existence of files named in columns identified as 'additional_files' columns.
                    missing_additional_files = False
                    for additional_file_field in self.field_configs["additional_files"]:
                        additional_file_value = row[additional_file_field].strip()
                        if len(additional_file_value) == 0:
                            message = ("CSV row with ID " + row[self.ctx["config"][
                                "id_field"]] + ' contains an empty value in its "' + additional_file_field + '" column.')
                            self.ctx["log"].warning(message)
                            row_level_warnings.append(message)
                        if not check_file_exists(self.ctx["config"], additional_file_value):
                            missing_additional_files = True
                            message = ('Additional file "' + additional_file_value
                                       + '" in CSV column "' + additional_file_field + '" in row with ID ' +
                                       row[self.ctx["config"][
                                           "id_field"]] + " not found or not accessible.")
                            self.ctx["log"].error(message)
                            if self.ctx["config"]["allow_missing_files"] is False:
                                row_level_errors.append(message)


                    if missing_additional_files is True:
                        if self.ctx["config"]["allow_missing_files"] is True:
                            message = '"allow_missing_files" configuration setting is set to "true", and "additional_files" CSV columns containing missing files were detected.'
                            self.ctx["log"].warning(message + " Details are logged above.")
                    else:
                        message = 'OK, files named in "additional_files" CSV columns are all present.'
                        self.ctx["log"].info(message)
            else:  # Either "paged_content_from_directories" or "paged_content_from_directories_parents_exist" is True.
                if "file" in row:
                    file_name = row["file"].strip()
                    if len(file_name) == 0:
                        message = ("CSV row with ID " + row[
                            self.ctx["config"]["id_field"]] + ' contains an empty "file" value.')
                        self.ctx["log"].warning(message)
                        row_level_warnings.append(message)
                    else:
                        if not ping_remote_file(self.ctx["config"], file_name):
                            message = ('Remote file "' + file_name + '" identified in CSV "file" column for row with ID "' +
                                       row_id + '" not found or not accessible.')
                            self.ctx["log"].error(message)
                            if self.ctx["config"]["allow_missing_files"] is False:
                                row_level_errors.append(message)

                dir_path = os.path.join(
                    self.ctx["config"]["input_dir"], row[self.ctx["config"]["page_files_source_dir_field"]]
                )
                if not os.path.exists(dir_path) or os.path.isfile(dir_path):
                    message = f"Page directory {dir_path} for CSV record with ID \"{row_id}\" not found."
                    self.ctx["log"].error(message)
                    raise WorkbenchValidationException(message)

                page_files = os.listdir(dir_path)
                if len(page_files) == 0:
                    message = f"Page directory {dir_path} for CSV record with ID \"{row_id}\" is empty."
                    self.ctx["log"].warning(message)

                paged_content_sequence_indicator_warnings = False
                additional_page_media_no_utf8_warnings = list()
                for page_file_name in page_files:
                    # Only want files, not directories.
                    if os.path.isdir(os.path.join(dir_path, page_file_name)):
                        continue

                    if paged_content_ignore_file(self.ctx["config"], page_file_name):
                        self.ctx["log"].info(
                            f'Ignoring file "{os.path.join(dir_path, page_file_name)}" since it matches an entry in the "paged_content_ignore_files" config setting.'
                        )
                        continue
                    else:
                        if self.ctx["config"]["paged_content_sequence_separator"] not in page_file_name:
                            message = ("Page file " + os.path.join(
                                dir_path, page_file_name
                                ) + " does not contain a sequence separator (" + self.ctx["config"]["paged_content_sequence_separator"] + ").")
                            self.ctx["log"].warning(message, log_only=True)
                            paged_content_sequence_indicator_warnings = True
                        page_sequence_indicator = get_sequence_indicator_from_filename(
                            self.ctx["config"], page_file_name
                        )
                        if not validate_weight_value(page_sequence_indicator):
                            self.ctx["log"].warning(
                                f'Sequence indicator in page filename "{os.path.join(dir_path, page_file_name)}" is not a valid "field_weight" value.',
                                log_only=True
                            )
                            paged_content_sequence_indicator_warnings = True

                    # Check additional page media files (e.g. OCR and hOCR files) for utf8 encoding.
                    if "paged_content_additional_page_media" in self.ctx["config"]:
                        for extension_mapping in self.ctx["config"]["paged_content_additional_page_media"]:
                            for additional_page_media_use_term, additional_page_media_extension in extension_mapping.items():
                                page_file_base_path, page_file_extension = os.path.splitext(page_file_name)
                                if page_file_extension.lstrip(".") == additional_page_media_extension:
                                    if check_file_exists(self.ctx["config"], page_file_name):
                                        if not file_is_utf8(page_file_name):
                                            message = (
                                                'Additional page/child media file "' + page_file_name + '" in directory for row ID "' +
                                                row_id + '" is not encoded as UTF-8 so will not be ingested.'
                                            )
                                            if page_file_name not in additional_page_media_no_utf8_warnings:
                                                self.ctx["log"].warning(message, log_only=True)
                                                additional_page_media_no_utf8_warnings.append(page_file_name)
                if paged_content_sequence_indicator_warnings:
                    self.ctx["log"].info(
                        "Warning: Check your Workbench log for entries about sequence indicator/field_weight values for page/child files.",
                        print_only=True
                    )
                if len(additional_page_media_no_utf8_warnings) > 0:
                    self.ctx["log"].info(
                        "Warning: Check your Workbench log for entries about UTF-8 encoding of additional page/child files.",
                        print_only=True
                    )

        if "image_alt_text" in row:
            if len(row["image_alt_text"]) > self.ctx["config"]["max_image_alt_text_length"]:
                image_alt_text = row["image_alt_text"]
                max_alt_text_length = self.ctx["config"]["max_image_alt_text_length"]
                node_id = row_id
                message = f"Alt text in input CSV row with node ID {node_id} is longer than the maximum configured alt text length ({max_alt_text_length})"
                self.ctx["log"].warning(
                    message + ". See log for more information.", log_message=
                    message + f" (length is {len(image_alt_text)} characters). Adding the alt text in this row will be skipped."
                )

        # Validate geolocation fields
        for field_name in self.field_types["geolocation"]:
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_geolocation_fields_action(self.ctx["config"], row, field_name)
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(str(e), log_only=True)
                    row_level_errors.append(str(e))

        # Validate link fields
        for field_name in self.field_types["link"]:
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_link_fields_action(self.ctx["config"], row, field_name)
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(str(e), log_only=True)
                    row_level_errors.append(str(e))

        # Validate authority link fields
        for field_name in self.field_types["authority_link"]:
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_authority_link_value_action(self.ctx["config"], self.field_configs["entity_field_definitions"][field_name]["authority_sources"], row, field_name)
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(str(e), log_only=True)
                    row_level_errors.append(str(e))

        # Validate dates in any other fields that are of type 'edtf'.
        for field_name in self.field_types["edtf"]:
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_node_created_date_action(self.ctx["config"], row, field_name)
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(str(e), log_only=True)
                    row_level_errors.append(str(e))

        # Taxonomy term references
        for field_name in self.field_types["taxonomy_term_reference"]:
            if field_name in row and row[field_name].strip() != "" and "vocabularies" in self.field_configs["entity_field_definitions"][field_name]:
                try:
                    validate_taxonomy_reference_value_action(self.ctx["config"], self.field_configs["entity_field_definitions"], field_name, row[field_name], row_number)
                except WorkbenchValidationException as e:
                    self.ctx["log"].warning(str(e), log_only=True)
                    row_level_warnings.append(str(e))

        # Cardinality
        for field_name in self.field_types["cardinality"]:
            if field_name in row and row[field_name].strip() != "":
                cardinality_value = self.field_configs["entity_field_definitions"][field_name]["cardinality"]
                delimited_field_values = row[field_name].split(
                    self.ctx["config"]["subdelimiter"]
                )
                if len(delimited_field_values) > cardinality_value:
                    message = (
                            f'CSV field "{field_name}" in record with ID {row_id} contains more values ' + f'than the number of values allowed for that field (max {cardinality_value}). Workbench with add only the first{"" if cardinality_value == 1 else f" {cardinality_value}"} value(s) for that field.')
                    row_level_warnings.append(message)
                    self.ctx["log"].warning(message, log_only=True)

        # Field max length
        for field_name, max_length in self.field_types["max_length"].items():
            if max_length is not None and field_name in row and row[field_name].strip() != "":
            # We don't care about max_length of None (i.e., it's not applicable or unlimited).
                delimited_field_values = row[field_name].split(self.ctx["config"]["subdelimiter"])
                for field_value in delimited_field_values:
                    if len(field_value) > int(max_length):
                        message = ('CSV field "' + field_name + '" in record with ID ' + row[
                            self.ctx["config"]["id_field"]] + " contains a value that is longer (" + str(
                            len(field_value)
                            ) + " characters)" + " than allowed for that field (" + str(
                            max_length
                            ) + " characters). Workbench will truncate this value prior to populating Drupal."
                        )
                        row_level_warnings.append(message)
                        self.ctx["log"].warning(message, log_only=True)

        # Validate allowed values
        for field_name, allowed_values in self.field_types["allowed_values"].items():
            if field_name in row and row[field_name].strip() != "":
                delimited_field_values = row[field_name].split(self.ctx["config"]["subdelimiter"])
                for field_value in delimited_field_values:
                    if field_value not in allowed_values:
                        message = ('CSV field "' + field_name + '" in record with ID ' + row[self.ctx["config"][
                            "id_field"]] + ' contains a value ("' + field_value + "\") that is not in the fields's allowed values.")
                        self.ctx["log"].warning(message, log_only=True)
                        row_level_warnings.append(message)

        # Validate typed relation fields
        for field_name in self.field_types["typed_relation"]:
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_typed_relation_field_value_action(
                        self.ctx["config"],
                        self.field_configs["entity_field_definitions"],
                        field_name,
                        row[field_name],
                        row_number
                    )
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(str(e), log_only=True)
                    row_level_errors.append(str(e))

        # Validate numeric fields
        for field_name, is_decimal in self.field_types["numeric_fields"].items():
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_numeric_field_action(self.ctx["config"], field_name, row[field_name], is_decimal)
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(str(e), log_only=True)
                    row_level_errors.append(str(e))

        # Validate media track fields
        for field_name, media_bundle_name in self.field_types["media_fields"].items():
            if field_name in self.field_configs["media_bundle_field_definitions"][media_bundle_name]:
                if self.field_configs["media_bundle_field_definitions"][media_bundle_name][field_name]["field_type"] == "media_track":
                    field_values = row[field_name].split(self.ctx["config"]["subdelimiter"])
                    for field_value in field_values:
                        if len(field_value.strip()) > 0:
                            if not validate_media_track_value(field_value):
                                message = (
                                    f'Value in field "{field_name}" in row with ID "{row_id}" ({field_value}) has a media type is not a valid media track field value.'
                                )
                                self.ctx["log"].error(message, log_only=True)
                                row_level_errors.append(message)
            else:
                message = (
                    f'Field "{field_name}" in row with ID "{row_id}" is not defined in the media bundle "{media_bundle_name}".'
                )
                self.ctx["log"].error(message, log_only=True)
                row_level_errors.append(message)

        # Validate parent nodes exist
        if self.ctx["config"]["validate_parent_node_exists"] is True and "field_member_of" in csv_column_headers and row["field_member_of"].strip() != "":
            parent_nids = row["field_member_of"].split(self.ctx["config"]["subdelimiter"])
            for parent_nid in parent_nids:
                if len(parent_nid) > 0:
                    if not ping_node(
                        self.ctx["config"], parent_nid, warn=False
                    ):
                        log_message = ("The 'field_member_of' field in row with ID '" + row[self.ctx["config"][
                            "id_field"]] + "' of your CSV file contains a node ID (" + parent_nid + ") that " + "doesn't exist or is not accessible. See the workbench log for more information.")
                        message = f'Node identified in "field_member_of" ({parent_nid}) in row with ID "{row_id}" cannot be found or accessed.'
                        self.ctx["log"].error(log_message=log_message)
                        row_level_errors.append(message)
        else:
            message = (
                    '"validate_parent_node_exists" is set to false. Node IDs in "field_member_of" that do not exist or are not accessible ' + 'will result in 422 errors in "create" and "update" tasks.')
            self.ctx["log"].warning(message, log_only=True)

        if self.testing_flags["langcode_was_present"]:
            if not validate_language_code(row["langcode"]):
                message = (f"Row with ID " + row[self.ctx["config"][
                    "id_field"]] + " of your CSV file contains an invalid Drupal language code (" + row[
                               "langcode"] + ") in its 'langcode' column.")
                self.ctx["log"].error(message, log_only=True)
                row_level_errors.append(message)

        # Validate

        if len(row_level_errors) > 0:
            self.ctx["log"].error(f"Row {row_number} has validation errors:", print_only=True)
            for error in row_level_errors:
                self.ctx["log"].error(error, print_only=True)
            raise WorkbenchValidationException(f"Row {row_number} has validation errors")
        if len(row_level_warnings) > 0:
            self.ctx["log"].warning(f"Row {row_number} has validation warnings:", print_only=True)
            for warning in row_level_warnings:
                self.ctx["log"].warning(warning, print_only=True)
        else:
            self.ctx["log"].info(f"Row {row_number} passed validation checks.", print_only=True)


    def prepare_run(self):
        # Prepare for the create task (e.g., validate input, set up environment)
        self.ctx["log"].info(
            f'Using the CSV ID to node ID map at "{self.ctx["config"]["csv_id_to_node_id_map_path"]}".'
        )
        if (
                self.ctx["config"]["recovery_mode_starting_from_node_id"] is not False
                and value_is_numeric(self.ctx["config"]["recovery_mode_starting_from_node_id"])
        ):
            mapping_count = self.ctx["node_map"].get_count()
            if len(mapping_count) > 0:
                last_node_id, last_csv_id = self.ctx["node_map"].get_last_ids()
                if self.ctx["config"]["paged_content_from_directories"] is True:
                    in_map_message = (
                        f'Nodes for {mapping_count} rows (including child pages) in the input CSV "{self.ctx["config"]["input_csv"]}" have already been created ' + f'(last entry in the CSV ID to node ID map is for CSV row "{last_csv_id}" at {self.ctx["config"]["host"]}/node/{last_node_id}).'
                    )
                else:
                    in_map_message = (
                        f'Nodes for {mapping_count} rows in the input CSV "{self.ctx["config"]["input_csv"]}" have already been created ' + f'(last entry in the CSV ID to node ID map is for CSV row "{last_csv_id}" at {self.ctx["config"]["host"]}/node/{last_node_id}).'
                    )
            else:
                in_map_message = f'Cannot find any entries in the CSV ID to node ID map created using the configuration file "{self.ctx["config"]["config_file"]}".'
            message = (
                    f'"recovery_mode" option in effect. Items that have already been ingested with node IDs starting at ' + f'{self.ctx["config"]["recovery_mode_starting_from_node_id"]} will be skipped.')
            self.ctx["log"].info(message)
            self.ctx["log"].info(in_map_message)
        message = f'"Create" task started using config file {self.ctx["config"]["config_file"]}.'
        self.ctx["log"].info(message)

        check_rollback_file_path_directories(self.ctx["config"])

        if self.ctx["config"]["ignore_existing_parent_ids"] is True:
            workbench_execution_start_time = "{:%Y-%m-%d %H:%M:%S}".format(
                datetime.datetime.now()
            )

        else:
            message = "'ignore_existing_parent_ids' is set to false, parent IDs from previous Workbench sessions will be used."
            self.ctx["log"].info(message)

        # If this is a secondary task, use the primary tasks execution start time.
        if os.environ.get("ISLANDORA_WORKBENCH_SECONDARY_TASKS") is not None:
            if os.path.abspath(self.ctx["config"]["config_file"]) in json.loads(
                    os.environ["ISLANDORA_WORKBENCH_SECONDARY_TASKS"]
            ):
                # Ensure that query_csv_id_to_node_id_map_for_parents is True.
                self.ctx["config"]["query_csv_id_to_node_id_map_for_parents"] = True
                workbench_execution_start_time = os.environ.get(
                    "ISLANDORA_WORKBENCH_PRIMARY_TASK_EXECUTION_START_TIME"
                )

        self.ctx["rollback"] = RollbackMgr(self.ctx)
        if self.ctx["config"]["progress_bar"] is True:
            self.ctx["progress_bar"] = InitBar(size=self.ctx["csv"].row_count, title=f"Running {self.name} task")


        if self.ctx["config"]["csv_headers"] == "labels":
            fieldname_map_cache_path = os.path.join(
                self.ctx["config"]["temp_dir"], f"node-{self.ctx['config']['content_type']}-labels.fieldname_map"
            )
            if os.path.exists(fieldname_map_cache_path):
                os.remove(fieldname_map_cache_path)

        if self.ctx["config"]["log_term_creation"] is False:
            self.ctx["log"].info(
                "'log_term_creation' configuration setting is False. Creation of new taxonomy terms will not be logged."
            )

        if "parent_id" in self.ctx["csv"].field_names and self.ctx["config"]["query_csv_id_to_node_id_map_for_parents"] is False and self.ctx["config"]["recovery_mode_starting_from_node_id"] is False:
            message = "Only node IDs for parents created during this session will be used (not using the CSV ID to node ID map)."
            self.ctx["log"].warning(message)

        if self.ctx["config"]["query_csv_id_to_node_id_map_for_parents"] is True and self.ctx["config"][
            "ignore_duplicate_parent_ids"] is True:
            message = "Ignoring duplicate parent IDs in the CSV ID to node ID map; only the most recent entries will be used."
            self.ctx["log"].warning(message)

        if self.ctx["config"]["nodes_only"] is True:
            self.ctx["log"].info('"nodes_only" option in effect. No media will be created.')

        if self.ctx["config"]["paged_content_from_directories_parents_exist"] and "field_member_of" not in self.ctx["csv"].field_names:
            message = '"field_member_of" is a required column in your input CSV when using the "paged_content_from_directories_parents_exist: true" configuration setting.'
            self.ctx["log"].error(message)
            raise WorkbenchException(message)


    def process_row(self, row: Union[dict, str], row_number: int):
        # Implement the logic to process each row for the create task
        if isinstance(row, str):
            self.ctx["log"].error(f"Row {row_number} is a string, expected a dictionary. Skipping this row.")
            return

        if (
            self.ctx["config"]["recovery_mode_starting_from_node_id"] is not False
            and value_is_numeric(self.ctx["config"]["recovery_mode_starting_from_node_id"]) is True
        ):
            # This is recovery mode specific code.
            nid_in_map = self.ctx["node_map"].get_node_id_recovery_mode(row[self.ctx["config"]["id_field"]])
            if nid_in_map is not False:
                message = f"Item in row {row[self.ctx['config']['id_field']]} has already been ingested at ({self.ctx['config']['host']}/node/{nid_in_map}), skipping it."
                self.ctx["log"].info(message)

                # Recover paged content from directories that have the current node as their parent.
                if self.ctx["config"]["paged_content_from_directories"] is True:
                    self.create_children_from_directory(row, nid_in_map)

                return

            # If we have gotten this far, the current row has not been created in Drupal.
            if "parent_id" in self.ctx["csv"].field_names and len(row["parent_id"]) > 0:
                parent_in_id_map_result = self.ctx["node_map"].get_node_id_from_recovery_mode(row["parent_id"])
                if len(parent_in_id_map_result) > 0:
                    row["field_member_of"] = str(parent_in_id_map_result[0][0])
                else:
                    message = f'Recovery mode was unable to find the node ID for the parent CSV row "{row["parent_id"]}" in the CSV ID to node ID map.'
                    if self.ctx["config"]["exit_on_recovery_mode_parent_lookup_failure"] is True:
                        self.ctx["log"].error(message)
                        raise WorkbenchException(message)
                    else:
                        self.ctx["log"].error(message)

        if (
                "node_exists_verification_view_endpoint" in self.ctx["config"]
                and self.ctx["config"]["paged_content_from_directories_parents_exist"] is False
                and len(self.ctx["config"]["node_exists_verification_view_endpoint"]) > 0
        ):
            candidate_node_id = verify_node_exists_by_key(self.ctx["config"], copy.copy(row))
            if candidate_node_id is not False:
                message = f"Item in row {row[self.ctx['config']['id_field']]} appears to already be in Drupal ({self.ctx['config']['host']}/node/{candidate_node_id}), skipping it."
                self.ctx["log"].warning(message)
                return

        # todo: remove these if they aren't used.
        # Create a copy of the current item's row to pass to create_media().
        row_for_media = copy.deepcopy(row)
        # Create a copy of the current item's row to pass to the
        # create_children_from_directory function.
        row_as_parent = copy.deepcopy(row)

        id_field = row[self.id_field]
        unpopulated_member_of_log_message = None

        # Add required fields.
        row["title"] = truncate_csv_value(
            "title", id_field, self.field_configs["entity_field_definitions"]["title"], row["title"]
        )
        node = {"type": [{"target_id": self.ctx["config"]["content_type"], "target_type": "node_type"}],
            "title": [{"value": row["title"]}], }

        # Some optional node base fields.
        optional_fields = {
            "uid": "target_id",
            "created": "value",
            "langcode": "value",
            "status": "value",
            "promote": "value",
        }
        for field, output_key in optional_fields.items():
            if field in self.ctx["csv"].field_names:
                if len(row[field]) > 0:
                    node[field] = [{output_key: row[field]}]
                # Reset it to empty so it doesn't throw a key error in the code
                # in the "Assemble Drupal field structures..." section below.
                row[field] = ""

        if "field_member_of" in self.ctx["csv"].field_names and (len(row["field_member_of"]) > 0):
            # First split out subvalues.
            field_member_of_values = []
            if self.ctx["config"]["subdelimiter"] in row["field_member_of"]:
                field_member_of_values = row["field_member_of"].split(
                    self.ctx["config"]["subdelimiter"]
                )
            else:
                field_member_of_values.append(row["field_member_of"])

            field_member_of_values_nids = []
            for field_member_of_value in field_member_of_values:
                if not value_is_numeric(field_member_of_value):
                    # Get the node ID for each item in the list.
                    field_member_of_value_nid = get_nid_from_url_alias(
                        self.ctx["config"], field_member_of_value
                    )
                    if field_member_of_value_nid is False:
                        message = f'Node identified in "field_member_of" ({field_member_of_value}) in CSV row with ID "{id_field}" cannot be found or accessed.'
                        self.ctx["log"].warning(message)
                    else:
                        field_member_of_values_nids.append(
                            str(field_member_of_value_nid)
                        )
                else:
                    field_member_of_values_nids.append(str(field_member_of_value))
            row["field_member_of"] = self.ctx["config"]["subdelimiter"].join(
                field_member_of_values_nids
            )

        # We want to collect the node IDs of items that are parents in-session
        # to accommodate users who have 'query_csv_id_to_node_id_map_for_parents: false'
        # in their config files. Note: this will not work with secondary tasks, if
        # you're using a secondary task you need to use the CSV ID to node ID map.
        # If a node with an ID that matches the current item's 'parent_id'
        # value has just been created, make the item a child of the node.
        #
        # EDIT: The new NodeIdMap supports an in-memory map which uses the same syntax.
        if "parent_id" in self.ctx["csv"].field_names and row["parent_id"] is not None and len(row["parent_id"]) > 0:
            parent_node_id = self.ctx["node_map"].get_node_id(row["parent_id"])
            if parent_node_id is None:
                unpopulated_member_of_log_message = f'CSV row ({row_number} with ID "{id_field}" has a "parent_id" value of "{row["parent_id"]}" that does not match any node IDs in the CSV ID to node ID map.'
                self.ctx["log"].warning(unpopulated_member_of_log_message)
                # todo: should we raise an exception here?
            elif isinstance(parent_node_id, list):
                message = f'Review your Workbench log for problems with the "parent_id" value in row with ID "{id_field}" in your input CSV data.'
                unpopulated_member_of_log_message = (
                        f"Row ({row_number} with ID \"{id_field}\" has a \"parent_id\" value (\"{row['parent_id']}\") that corresponds to more than one node ID in the CSV ID to node ID " + f"map (corresponding node IDs in the map are {', '.join(parent_node_id)}). Workbench cannot reliably determine the child node's (CHILDNODEID) parent " + 'node ID and will not populate node CHILDNODEID\'s "field_member_of".')
                self.ctx["log"].warning(message, log_message=unpopulated_member_of_log_message)
                row["field_member_of"] = parent_node_id[0]
            else:
                row["field_member_of"] = parent_node_id

        # Only add config['id_field'] to required_fields if it is not a node field.
        required_fields = ["file", "title"]
        if self.id_field not in self.field_configs["entity_fields"]:
            required_fields.append(self.id_field)
        custom_fields = list(set(self.ctx["csv"].field_names) - set(required_fields))

        for custom_field in custom_fields:
            # Skip processing field if empty. The only exception is if a field is registered
            # in the config to have a preprocessor applied to it, since preprocessors can populate
            # empty CSV values.
            if "preprocessors" in self.config_keys and len(self.ctx["config"]["preprocessors"]) > 0:
                for preprocessor in self.ctx["config"]["preprocessors"]:
                    # The following two patterns are valid: /fieldname: interpreter script_path/, and /fieldname: script_path/.
                    # Also, on Windows, both interpreter and script_path can contain colons.
                    for (
                        processor_field_name,
                        processor_script_path,
                    ) in preprocessor.items():
                        if custom_field == processor_field_name:
                            row[custom_field] = preprocessor_scripts_csv_row(
                                self.ctx["config"], row, custom_field, processor_script_path
                            )

            if len(str(row[custom_field]).strip()) == 0:
                continue

            if len(self.field_configs["additional_files"]) > 0 and custom_field in self.field_configs["additional_files"].keys():
                continue

            # This field can exist in the CSV to create parent/child
            # relationships and is not a Drupal field.
            # 'langcode' is a core Drupal field, but is not considered a "base field".
            # 'image_alt_text' is a reserved CSV field.
            # 'url_alias' is a reserved CSV field.
            # 'media_use_tid' is a reserved CSV field.
            # 'checksum' is a reserved CSV field.
            # 'directory' is a reserved CSV field.
            if custom_field in ["parent_id", "langcode", "image_alt_text", "url_alias", "media_use_tid", "checksum", "directory"]:
                continue

            # We skip CSV columns whose headers use the 'media:video:field_foo' media track convention.
            if custom_field.startswith("media:"):
                continue

            # Assemble Drupal field structures from CSV data. If new field types are added to
            # fields.py, they need to be registered in the following if/elif/else block.

            field = WorkbenchFieldFactory.get_field_handler(
                self.field_configs["entity_field_definitions"][custom_field]["field_type"]
            )
            node = field.create(self.ctx["config"], self.field_configs["entity_field_definitions"], node, row, custom_field)

            # If the user has configured Workbench to not query the CSV ID to node ID map,
            # use the in-session node_ids list to track and assign parent node IDs.
            #
            # Now we are using an in-memory sqlite3 database to track node IDs, so this code is no longer needed.
            #if config["query_csv_id_to_node_id_map_for_parents"] is False:
            #    if "parent_id" in row and row["parent_id"] in node_ids.keys():
            #        node["field_member_of"] = [
            #            {
            #                "target_id": node_ids[row["parent_id"]],
            #                "target_type": "node_type",
            #            }
            #        ]

        if self.ctx["config"]["paged_content_from_directories_parents_exist"] is True:
            if ping_node(self.ctx["config"], row["field_member_of"], warn=False) is True:
                # Console output and logging are done in the create_children_from_directory() function.
                self.create_children_from_directory(
                    row_as_parent, row["field_member_of"]
                )
            else:
                message = f'Node {row["field_member_of"]} identified in the "field_member_of" column in your input CSV doesn\'t exist, skipping adding children.'
                self.ctx["log"].warning(message)

            # We don't want to create the parent node, it already exists.
            return

        node_create_json, node_create_meta = create_node(self.ctx["config"], node)
        if node_create_meta["status_code"] == 201:
            node_id = node_create_json["nid"][0]["value"]
            node_uri = self.ctx["config"]["host"] + "/node/" + str(node_id)

            if ("parent_id" in row and row["parent_id"] is not None and self.ctx["config"][
                "query_csv_id_to_node_id_map_for_parents"] is True):
                self.ctx["node_map"].add_mapping(
                    row["parent_id"], row["field_member_of"], id_field, node_id
                )
            else:
                self.ctx["node_map"].add_mapping("", "", id_field, node_id)


            file_column = row["file"] if "file" in row else ""
            member_of = row["field_member_of"] if "field_member_of" in row else ""

            self.ctx["rollback"].write_rollback_node_id(
                node_id, row[self.id_field], row["title"], file_column, member_of
            )

            if unpopulated_member_of_log_message is not None:
                unpopulated_member_of_log_message = (unpopulated_member_of_log_message.replace(
                    "CHILDNODEID", str(node_id)
                ))
                self.ctx["log"].warning(unpopulated_member_of_log_message)

            if self.ctx["config"]["progress_bar"] is False:
                row_position_message = ""
                if self.ctx["config"]["show_percentage_of_csv_input_processed"] is True:
                    row_position = int(row_number) / int(self.ctx["csv"].row_count) * 100
                    row_position_message = f" ({int(row_position)}%)"
                self.ctx["log"].info(
                    f'Node for "{row["title"]}" (record {id_field}) created at {node_uri}{row_position_message}.'
                )

            self.ctx["log"].info(
                'Node for "%s" (record %s) created at %s.', row["title"], id_field, node_uri, )
            if "output_csv" in self.ctx["config"].keys():
                # We pass a copy of the row into this function because Python.
                self.ctx["output_csv_writer"].write_to_output_csv(
                    id_field, node_create_json, copy.deepcopy(row)
                )

            # I AM MOVING THIS INTO A VALID NODE CREATION BLOCK ABOVE, SINCE IT SHOULD ONLY HAPPEN IF THE NODE WAS CREATED SUCCESSFULLY.
            #
            # Execute node-specific post-create scripts, if any are configured.
            if "node_post_create" in self.config_keys and len(self.ctx["config"]["node_post_create"]) > 0:
                for command in self.ctx["config"]["node_post_create"]:
                    (post_task_output, post_task_return_code,) = execute_entity_post_task_script(
                        command, self.ctx["config"]["config_file"], node_create_meta["status_code"], json.dumps(node_create_json))
                    if post_task_return_code == 0:
                        self.ctx["log"].info(
                            f"Post node create script {command} executed successfully."
                        )
                    else:
                        self.ctx["log"].error(f"Post node create script {command} failed.")

            if self.ctx["config"]["progress_bar"] is True:
                self.ctx["progress_bar"].updateAmount(row_number)

            # If 'url_alias' is in the CSV, create the alias.
            if "url_alias" in row and len(row["url_alias"]) > 0:
                create_url_alias(self.ctx["config"], node_id, row["url_alias"])

            # If the file named in 'file' can't be found.
            if "file" in row and len(row["file"].strip()) > 0:
                if (
                        self.ctx["config"]["nodes_only"] is False
                        and self.ctx["config"]["paged_content_from_directories"] is False
                        and check_file_exists(self.ctx["config"], row["file"].strip()) is False
                ):
                    message = (
                            f"No media for {node_uri} created since the file named the input CSV's \"file\" column (row with ID '{id_field}') could not be found."
                    )
                    if self.ctx["config"]["allow_missing_files"] is False:
                        self.ctx["log"].error(message)
                        raise WorkbenchException(message)
                        # todo: Should we check this earlier and skip the entire node creation?
                    elif self.ctx["config"]["progress_bar"] is False:
                            self.ctx["log"].warning(message)
                    return
            else:
                message = (
                        f"No media for {node_uri} created since its \"file\" column in the input CSV (row with ID '{id_field}') is empty."
                    )
                self.ctx["log"].warning(message)


            allowed_media_response_codes = [201, 204]
            if (
                self.ctx["config"]["nodes_only"] is False
                and "file" in row
                and len(row["file"].strip()) != 0
            ):
                media_response_status_code = create_media(
                    self.ctx["config"], row["file"], "file", node_id, row_for_media
                )
                if media_response_status_code in allowed_media_response_codes:
                    self.ctx["log"].info("+ Media for " + row["file"] + " created.", log_only=self.ctx["config"]["progress_bar"])
                else:
                    self.ctx["log"].error(
                        f"Media for {row['file']} not created (HTTP response code {media_response_status_code}).",
                        log_only=self.ctx["config"]["progress_bar"]
                    )

            if self.ctx["config"]["nodes_only"] is False and "additional_files" in self.config_keys:

                if len(self.field_configs["additional_files"]) > 0:
                    for (
                        additional_file_field,
                        additional_file_media_use_tid,
                    ) in self.field_configs["additional_files"].items():
                        # If there is no additional media file, move on to the next "additional_files" column.
                        if additional_file_field not in row:
                            return

                        if (
                            additional_file_field in row
                            and len(row[additional_file_field].strip()) == 0
                        ):

                            message = (
                                f'Media for "additional_files" CSV column "{additional_file_field}" in row with ID "{row[self.id_field]}" '
                                + f'(node URL "{node_uri}") not created because CSV field is empty.'
                            )
                            if self.ctx["config"]["allow_missing_files"] is False:
                                self.ctx["log"].error(message)
                            else:
                                self.ctx["log"].warning(message)
                            return

                        filename = row[additional_file_field].strip()
                        file_exists = check_file_exists(self.ctx["config"], filename)
                        if not file_exists:
                            message = f'Media for file "{filename}" named in field "{additional_file_field}" of CSV row with ID "{row[self.id_field]}" not created because file does not exist.'
                            self.ctx["log"].error(
                                message,
                                log_only=self.ctx["config"]["progress_bar"]
                            )
                            if self.ctx["config"]["allow_missing_files"] is False:
                                raise WorkbenchException(message)
                            else:
                                return

                        media_response_status_code = create_media(
                            self.ctx["config"],
                            row[additional_file_field],
                            additional_file_field,
                            node_id,
                            row_for_media,
                            additional_file_media_use_tid,
                        )
                        if media_response_status_code in allowed_media_response_codes:
                            self.ctx["log"].info(
                                f"Media for {row[additional_file_field]} created.",
                                log_only=self.ctx["config"]["progress_bar"]
                            )
                        else:
                            message = f"Media for {row[additional_file_field]} not created (HTTP response code {media_response_status_code})."
                            self.ctx["log"].error(
                                message,
                                log_only=self.ctx["config"]["progress_bar"]
                            )


            elif (
                self.ctx["config"]["nodes_only"] is False
                and "file" in row
                and len(row["file"]) == 0
                and "additional_files" not in self.config_keys
                and self.ctx["config"]["paged_content_from_directories"] is False
            ):
                self.ctx["log"].info(
                    f"No files specified in CSV for row {id_field}, so no media created.",
                    log_only=self.ctx["config"]["progress_bar"]
                )

            elif self.ctx["config"]["paged_content_from_directories"] is True:
                # Console output and logging are done in the create_children_from_directory() function.
                self.create_children_from_directory(row_as_parent, node_id)

        else:
            self.ctx["log"].error(f"Node for CSV record {id_field} not created. See log for more information.")
            return

    def finish(self):
        # Finalize the create task (e.g., summarize results, clean up)
        pass


    def create_children_from_directory(
        self, parent_csv_record: dict, parent_node_id: str
    ):

        # These objects will have a title (derived from the page filename), an ID based on the parent's id, and a config-defined
        # Islandora model. Content type and status are inherited as is from parent, as are other required fields. Fields
        # specified in the csv_value_templates_for_paged_content config setting are also applied to paged children. The
        # weight assigned to the page is the last segment in the filename, split from the rest of the filename using the
        # character defined in the 'paged_content_sequence_separator' config option.
        parent_id = parent_csv_record[self.id_field] or ""
        page_dir_name = parent_csv_record[self.ctx["config"]["page_files_source_dir_field"]]
        page_dir_path = os.path.join(self.ctx["config"]["input_dir"], page_dir_name)

        if "paged_content_additional_page_media" in self.config_keys:
            if "paged_content_image_file_extension" in self.config_keys:
                page_files = [
                    f
                    for f in os.listdir(page_dir_path)
                    if f.endswith(
                        self.ctx["config"]["paged_content_image_file_extension"].lstrip(".").strip()
                    )
                ]
            else:
                page_files = os.listdir(page_dir_path)
        else:
            page_files = os.listdir(page_dir_path)

        # Identify any required fields that are in the parent CSV.

        for page_file_name in page_files:
            # Only want files, not directories.
            if os.path.isdir(os.path.join(page_dir_path, page_file_name)):
                continue

            if paged_content_ignore_file(self.ctx["config"], page_file_name):
                self.ctx["log"].info(
                    f'Ignoring file "{os.path.join(page_dir_path, page_file_name)}" since it matches an entry in the "paged_content_ignore_files" config setting.'
                )
                continue

            if (
                self.ctx["config"]["recovery_mode_starting_from_node_id"] is not False
                and value_is_numeric(self.ctx["config"]["recovery_mode_starting_from_node_id"])
                and parent_id is not None
            ):
                nid_in_map = self.ctx["node_map"].get_node_id_recovery_mode(page_file_name, parent_id)
                if nid_in_map is not False:
                    message = f"Page/child file {page_file_name} has already been ingested at ({self.ctx['config']['host']}/node/{nid_in_map}), skipping it."
                    self.ctx["log"].info(message)
                    continue

            weight = get_sequence_indicator_from_filename(self.ctx["config"], page_file_name)
            filename_without_extension = os.path.splitext(page_file_name)[0]

            # This "identifier" is the CSV ID, not a Drupal field. It may be overwritten
            # by a CSV value template below.
            page_identifier = str(parent_id) + "_" + filename_without_extension

            page_title = get_page_title_from_template(
                self.ctx["config"], parent_csv_record["title"], weight
            )

            inherited_fields = copy.copy(self.field_configs["required_bundle_fields"])

            csv_row_to_apply_to_paged_children = copy.deepcopy(parent_csv_record)
            csv_row_to_apply_to_paged_children["file"] = page_file_name
            if not validate_weight_value(weight) and not paged_content_ignore_file(self.ctx["config"], page_file_name):
                self.ctx["log"].warning(
                    f'Sequence indicator in page filename "{os.path.join(page_dir_path, page_file_name)}" is not a valid "field_weight" value; that field will not be populated on the page node.'
                )
                weight = "0"

            # The page's field_weight is assigned below, but we also include the assigned value
            # in the temporary CSV record in case any CSV value templates are applied.
            try:
                csv_row_to_apply_to_paged_children["field_weight"] = int(weight) * int(
                    self.ctx["config"]["paged_content_page_weight_multiplier"]
                )
            except ValueError:
                csv_row_to_apply_to_paged_children["field_weight"] = ""

            # Add any fields to the page's row that are defined in config["csv_value_templates_for_paged_content"].
            if (
                "csv_value_templates_for_paged_content" in self.config_keys
                and len(self.ctx["config"]["csv_value_templates_for_paged_content"]) > 0
            ):
                for paged_items_template in self.ctx["config"]["csv_value_templates_for_paged_content"]:
                    for field_name, template in paged_items_template.items():
                        if field_name not in inherited_fields and field_name in parent_csv_record:
                            inherited_fields.append(field_name)
                            csv_row_to_apply_to_paged_children[field_name] = parent_csv_record[field_name]

                csv_row_to_apply_to_paged_children = apply_csv_value_templates(
                    self.ctx["config"],
                    "csv_value_templates_for_paged_content",
                    csv_row_to_apply_to_paged_children,
                )

            # If a field is registered in the config to have a preprocessor applied to it
            # (including empty fields), apply the processor.
            if "preprocessors" in self.config_keys and len(self.ctx["config"]["preprocessors"]) > 0:
                for csv_field in csv_row_to_apply_to_paged_children.keys():
                    if csv_field in self.ctx["config"]["preprocessors"].keys():
                        csv_row_to_apply_to_paged_children[
                            csv_field
                        ] = preprocessor_scripts_csv_row(
                            self.ctx["config"],
                            csv_row_to_apply_to_paged_children,
                            csv_field,
                            self.ctx["config"]["preprocessors"][csv_field],
                        )

            node_json = {
                "type": [
                    {
                        "target_id": self.ctx["config"]["paged_content_page_content_type"],
                        "target_type": "node_type",
                    }
                ],
                "title": [{"value": page_title}],
                "field_member_of": [{"target_id": parent_node_id, "target_type": "node"}],
                "field_weight": [
                    {
                        "value": int(weight)
                        * int(self.ctx["config"]["paged_content_page_weight_multiplier"])
                    }
                ],
            }

            # Add field_model if that field exists in the child's content type.

            if "field_model" in self.field_configs["entity_fields"]:
                paged_model_tid = str(self.ctx["config"]["paged_content_page_model_tid"]).strip()
                if not value_is_numeric(paged_model_tid) and paged_model_tid.startswith("http"):
                    paged_content_model_tid = get_term_id_from_uri(
                        self.ctx["config"], paged_model_tid
                    )
                else:
                    paged_content_model_tid = paged_model_tid
                node_json["field_model"] = [
                    {"target_id": paged_content_model_tid, "target_type": "taxonomy_term"}
                ]

            # Add viewer override if defined in CSV or config.
            viewer_override_fieldname = self.ctx["config"]["viewer_override_fieldname"]
            if "paged_content_page_viewer_override" in self.config_keys:
                paged_viewer_override = str(self.ctx["config"]["paged_content_page_viewer_override"]).strip()
                if value_is_numeric(paged_viewer_override):
                    page_viewer_override_tid = paged_viewer_override
                else:
                    page_viewer_override_tid_info = get_all_representations_of_term(
                        self.ctx["config"],
                        vocab_id="islandora_display",
                        uri=paged_viewer_override,
                    )
                    try:
                        page_viewer_override_tid = page_viewer_override_tid_info["term_id"]
                    except TypeError:
                        message = (
                            f'Could not find all representations of the taxonomy term for the viewer override "{paged_viewer_override}".'
                        )
                        self.ctx["log"].error(message)
                        raise WorkbenchException(message)

                node_json[viewer_override_fieldname] = [
                   {"target_id": page_viewer_override_tid, "target_type": "taxonomy_term", }]

            elif viewer_override_fieldname in parent_csv_record:
                node_json[viewer_override_fieldname] = [
                    {
                        "target_id": parent_csv_record["field_viewer_override"],
                        "target_type": "taxonomy_term",
                    }
                ]


            # Some optional base fields, inherited from the parent object.
            if "uid" in parent_csv_record and len(parent_csv_record["uid"]) > 0:
                node_json["uid"] = [{"target_id": parent_csv_record["uid"]}]

            if "created" in parent_csv_record and len(parent_csv_record["created"]) > 0:
                node_json["created"] = [{"value": parent_csv_record["created"]}]

            if len(inherited_fields) > 0:
                # Importing the workbench_fields module at the top of this module with the
                # rest of the imports causes a circular import exception, so we do it here.
                import workbench_fields

                for inherited_field in inherited_fields:
                    # These fields are populated above.
                    if inherited_field in [
                        "title",
                        "field_model",
                        "uid",
                        "created",
                        self.ctx["config"]["viewer_override_fieldname"],
                    ]:
                        continue

                    # Assemble Drupal field structures from CSV data. If new field types are added to
                    # fields.py, they need to be registered in the following if/elif/else block.
                    field = workbench_fields.WorkbenchFieldFactory.get_field_handler(
                        self.field_configs["entity_field_definitions"][inherited_field]["field_type"]
                    )
                    node_json = field.create(
                        self.ctx["config"],
                        self.field_configs["entity_field_definitions"],
                        node_json,
                        csv_row_to_apply_to_paged_children,
                        inherited_field,
                    )

            node_response_json, node_response_meta = create_node(self.ctx["config"], node_json)
            if node_response_meta["status_code"] == 201:
                node_uri = node_response_meta["location"]
                self.ctx["log"].info(f'Node for child "{page_title}" created at {node_uri}.')
                if "output_csv" in self.ctx["config"].keys():
                    self.ctx["output_csv_writer"].write_row_to_output_csv(page_identifier, node_response_json)

                node_nid = get_nid_from_url_alias(self.ctx["config"], node_uri)

                self.ctx["node_map"].add_mapping(parent_id, parent_node_id, page_file_name, node_nid)

                page_file_path = os.path.join(page_dir_name, page_file_name)
                self.ctx["rollback"].write_rollback_node_id(
                    node_nid,
                    "",
                    page_title,
                    page_file_path,
                    parent_node_id,
                )

                fake_csv_record = dict()
                fake_csv_record["title"] = page_title
                fake_csv_record["file"] = page_file_path
                fake_csv_record[self.id_field] = parent_csv_record[self.id_field]
                media_response_status_code = create_media(
                    self.ctx["config"], page_file_path, "file", str(node_nid), fake_csv_record
                )

                if media_response_status_code in [201, 204]:
                    if media_response_status_code is False:
                        self.ctx["log"].error(
                            f"- ERROR: Media for {page_file_path} not created. See log for more information.",
                            log_message=f"Media for {page_file_path} not created. HTTP response code was {media_response_status_code}."
                        )
                        continue
                    else:
                        self.ctx["log"].info(f"Media for {page_file_path} created.")

                if (
                    self.ctx["config"]["paged_content_from_directories"] is True
                    or self.ctx["config"]["paged_content_from_directories_parents_exist"] is True
                ):
                    if "paged_content_additional_page_media" in self.ctx["config"]:
                        for extension_mapping in self.ctx["config"][
                            "paged_content_additional_page_media"
                        ]:
                            for (
                                additional_page_media_use_term,
                                additional_page_media_extension,
                            ) in extension_mapping.items():
                                if str(additional_page_media_use_term).startswith("http"):
                                    additional_page_media_use_tid = get_term_id_from_uri(
                                        self.ctx["config"], additional_page_media_use_term
                                    )
                                else:
                                    additional_page_media_use_tid = (
                                        additional_page_media_use_term
                                    )
                                page_file_base_path = os.path.splitext(page_file_path)[0]
                                additional_page_media_file_path = (
                                    page_file_base_path
                                    + "."
                                    + additional_page_media_extension.strip()
                                )
                                if check_file_exists(
                                    self.ctx["config"], additional_page_media_file_path
                                ):
                                    media_response_status_code = create_media(
                                        self.ctx["config"],
                                        additional_page_media_file_path,
                                        None,
                                        str(node_nid),
                                        fake_csv_record,
                                        media_use_tid=additional_page_media_use_tid,
                                    )
                                    if (
                                        media_response_status_code
                                        in [201, 204]
                                    ):
                                        if media_response_status_code is False:
                                            self.ctx["log"].error(
                                                f"- ERROR: Media for {additional_page_media_file_path} not created. See log for more information.",
                                                log_message = f"Media for {additional_page_media_file_path} not created. HTTP response code was {media_response_status_code}."
                                            )
                                            continue
                                        else:
                                            self.ctx["log"].info(
                                                f"+ Media for {additional_page_media_file_path} created."
                                            )
                                else:
                                    self.ctx["log"].warning(
                                        f"{additional_page_media_file_path} not found."
                                    )

            else:
                self.ctx["log"].error(
                    f"Node for page {page_identifier} not created. See log for more information.",
                    log_message=f'Node for page "{page_identifier}" not created, HTTP response code was {node_response_meta["status_code"]}, response body was {node_response_json}.')
                self.ctx["log"].error(f'JSON request body used in previous POST for page "{page_identifier}" was {node_json}.', log_only=True)

            # Execute node-specific post-create scripts, if any are configured.
            if "node_post_create" in self.config_keys and len(self.ctx["config"]["node_post_create"]) > 0:
                for command in self.ctx["config"]["node_post_create"]:
                    post_task_output, post_task_return_code = (
                        execute_entity_post_task_script(
                            command,
                            self.ctx["config"]["config_file"],
                            node_response_meta["status_code"],
                            json.dumps(node_response_json),
                        )
                    )
                    if post_task_return_code == 0:
                        self.ctx["log"].info(
                            f"Post node create script {command} executed successfully."
                        )
                    else:
                        self.ctx["log"].error(
                            f"Post node create script {command} failed with exit code {post_task_return_code}."
                        )
