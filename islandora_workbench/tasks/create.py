import copy
import datetime
import os

from islandora_workbench.checks.validation import validate_url_aliases_action, validate_node_created_date_action, \
    validate_node_uid_action, validate_geolocation_fields_action, validate_link_fields_action, \
    validate_authority_link_value_action, validate_taxonomy_reference_value_action, \
    validate_typed_relation_field_value_action, validate_numeric_field_action, validate_weight_value
from islandora_workbench.tasks import register_task
from islandora_workbench.tasks.base import Task
from islandora_workbench.exceptions import WorkbenchValidationException, WorkbenchValidationWarning
from workbench_utils import get_entity_fields, is_running_in_docker, value_is_numeric, prepare_csv_id_to_node_id_map, \
    sqlite_manager, check_for_required_config_keys, check_rollback_file_path_directories, get_field_definitions, \
    get_additional_files_config, get_node_exists_verification_view_endpoint, ping_view_endpoint, \
    ping_entity_reference_view_endpoint, get_required_bundle_fields, validate_media_use_tid, check_file_exists, \
    get_file_hash_from_local, ping_node, validate_language_code, validate_required_fields_have_values_action, \
    validate_media_use_tids_in_csv_action, validate_media_track_value, ping_remote_file, \
    validate_media_use_tid_in_additional_files_setting, paged_content_ignore_file, get_sequence_indicator_from_filename, \
    file_is_utf8


@register_task("create")
class CreateTask(Task):

    name = "create"

    def __init__(self, ctx: dict):
        super().__init__(ctx)
        self.ctx = ctx

    def general_checks(self):
        self.field_configs["entity_fields"] = list(
            get_entity_fields(self.ctx['config'], "node", self.ctx['config']["content_type"]).keys()
        )
        self.field_configs["entity_field_definitions"] = get_field_definitions(self.ctx["config"], "node")
        if self.ctx['config']["id_field"] not in self.field_configs["entity_fields"]:
            self.field_configs["reserved_fields"].append(self.ctx['config']["id_field"])

        self.field_configs["additional_files"] = get_additional_files_config(self.ctx["config"])
        self.field_configs["required_bundle_fields"] = get_required_bundle_fields(self.ctx["config"], "node", self.ctx["config"]["content_type"])

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
            print("Warning: " + docker_message)
            self.ctx["log"].warning(docker_message)

        if (
            self.ctx["config"]["recovery_mode_starting_from_node_id"] is not False
            and value_is_numeric(self.ctx["config"]["recovery_mode_starting_from_node_id"]) is True
        ):
            message = f'"recovery_mode" option in effect. Items that have already been ingested with node IDs starting at {self.ctx["config"]["recovery_mode_starting_from_node_id"]} will be skipped.'
            print(message)
            self.ctx["log"].info(message)

            # Check for presence of required config keys, which varies by task.
            if self.ctx["config"]["nodes_only"] is True:
                self.ctx["log"].info('"nodes_only" option in effect. Media files will not be checked/validated.')

            try:
                check_for_required_config_keys(
                    self.config_keys, ["task", "host", "username", "password"], throw_exception=True
                )
            except KeyError as e:
                self.ctx["log"].error(f"Missing required configuration key: {e}")
                raise WorkbenchValidationException(f"Missing required configuration key: {e}")

            self.ctx["log"].info("OK, configuration file has all required values (did not check for optional values).")

        # Check that the rollback configuration file and CSV file directories exist and are writable.
        check_rollback_file_path_directories(self.ctx["config"])

    def general_csv_checks(self):
        """Perform general checks on the CSV file."""
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

            prepare_csv_id_to_node_id_map(self.ctx["config"])
            check_for_host_column_result = sqlite_manager(
                self.ctx["config"],
                operation="select",
                db_file_path=csv_to_node_id_map_path,
                query="select * from pragma_table_info(?)",
                values=("csv_id_to_node_id_map",),
            )
            if not isinstance(check_for_host_column_result, list):
                self.ctx["log"].warning("Unexpected result from SQLite query for table info in CSV ID to node ID map. Expected a list but got: " + str(check_for_host_column_result))
            elif check_for_host_column_result[-1][1] == "host":
                num_unique_hosts_result = sqlite_manager(
                    self.ctx["config"],
                    operation="select",
                    db_file_path=csv_to_node_id_map_path,
                    query="select distinct host from csv_id_to_node_id_map",
                )
                if not isinstance(num_unique_hosts_result, list):
                    self.ctx["log"].warning("Unexpected result from SQLite query for unique hosts in CSV ID to node ID map. Expected a list but got: " + str(num_unique_hosts_result))
                else:
                    unique_host_values = list()
                    for unique_host in num_unique_hosts_result:
                        if unique_host[0] is None or unique_host[0] == "":
                            unique_host_values.append("")
                        else:
                            unique_host_values.append(unique_host[0])

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

        if self.ctx["config"]["id_field"] not in csv_column_headers:
            message = f'For "create" tasks, your CSV file must have a column containing a unique identifier (column {self.ctx["config"]["id_field"]} missing).'
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

        if "title" not in csv_column_headers:
            message = 'For "create" tasks, your CSV file must contain a "title" column.'
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

        if (self.ctx["config"]["nodes_only"] is False and "file" not in csv_column_headers and self.ctx["config"][
            "paged_content_from_directories"] is False and self.ctx["config"][
            "paged_content_from_directories_parents_exist"] is False):
            message = 'For "create" tasks, your CSV file must contain a "file" column.'
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

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
        all_fields_check_list.extend(self.field_configs["node_base_fields"])
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
            node_exists_config = get_node_exists_verification_view_endpoint(self.ctx["config"])
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
        for entity_reference_view_field_name, entity_reference_view_endpoint in self.field_types[
            "entity_reference_view_endpoints"].items():
            if entity_reference_view_field_name not in csv_column_headers:
                message = f'CSV column {entity_reference_view_field_name} identified in "entity_reference_view_endpoints" is not in your CSV file.'
                self.ctx["log"].error(message)
                raise WorkbenchValidationException(message)
            view_url = f'{self.ctx["config"]["host"]}/{entity_reference_view_endpoint.lstrip("/")}'
            view_path_status_code = ping_view_endpoint(self.ctx["config"], view_url)
            if view_path_status_code != 200:
                message = f'Cannot access View REST export configured in "entity_reference_view_endpoints" ({view_url}).'
                self.ctx["log"].error(message)
                raise WorkbenchValidationException(message)
            else:
                message = f'View REST export configured in "entity_reference_view_endpoints" ({view_url}) is accessible.'
                self.ctx["log"].info(message)

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
                raise WorkbenchValidationException(message)

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



    def check_row(self, row: dict, row_number: int):
        """Check the current row for any issues."""
        csv_column_headers = copy.copy(self.ctx["csv"].field_names)
        row_id = row[self.ctx["config"]["id_field"]]
        row_level_errors = []

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
                        hash_from_local = get_file_hash_from_local(
                            self.ctx["config"], file_name, self.ctx["config"]["fixity_algorithm"]
                        )
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
                    self.ctx["log"].warning(str(e))

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
                    self.ctx["log"].warning(message)

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
                        self.ctx["log"].warning(message)

        # Validate allowed values
        for field_name, allowed_values in self.field_types["allowed_values"].items():
            if field_name in row and row[field_name].strip() != "":
                delimited_field_values = row[field_name].split(self.ctx["config"]["subdelimiter"])
                for field_value in delimited_field_values:
                    if field_value not in allowed_values:
                        message = ('CSV field "' + field_name + '" in record with ID ' + row[self.ctx["config"][
                            "id_field"]] + ' contains a value ("' + field_value + "\") that is not in the fields's allowed values.")
                        self.ctx["log"].warning(message)

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
        if self.ctx["config"]["validate_parent_node_exists"] is True:
            if "field_member_of" in csv_column_headers and row["field_member_of"].strip() != "":
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


    def prepare(self):
        # Prepare for the create task (e.g., validate input, set up environment)
        pass

    def process_row(self, row, row_number: int):
        # Implement the logic to process each row for the create task
        pass

    def finish(self):
        # Finalize the create task (e.g., summarize results, clean up)
        pass
