import datetime
import os
import sys

from islandora_workbench.tasks import register_task
from islandora_workbench.tasks.base import Task
from workbench_utils import get_entity_fields, is_running_in_docker, value_is_numeric, get_csv_data, \
    prepare_csv_id_to_node_id_map, sqlite_manager, check_for_required_config_keys, check_rollback_file_path_directories, \
    create_temp_dir, validate_input_dir, check_csv_file_exists, replace_field_labels_with_names, get_field_definitions, \
    validate_url_aliases, validate_parent_ids_precede_children, validate_parent_ids_in_csv_id_to_node_id_map, \
    get_additional_files_config, get_node_exists_verification_view_endpoint, ping_view_endpoint, \
    ping_entity_reference_view_endpoint, get_preprocessed_input_csv_file_path, get_required_bundle_fields, \
    validate_required_fields_have_values, validate_node_created_date, validate_node_uid, validate_media_use_tid, \
    validate_media_use_tids_in_csv, check_file_exists, get_file_hash_from_local, validate_geolocation_fields, \
    validate_link_fields, validate_authority_link_fields, validate_edtf_fields, validate_csv_field_cardinality, \
    validate_csv_field_length, validate_text_list_fields, validate_taxonomy_field_values, \
    validate_typed_relation_field_values, validate_numeric_fields, validate_media_track_fields, ping_node, \
    get_entity_reference_view_endpoints, validate_language_code


@register_task("create")
class CreateTask(Task):

    def __init__(self, ctx: dict):
        super().__init__(ctx)
        self.ctx = ctx

    def check(self):
        # Call parent class for setup
        super().check()

        if self.ctx.config["csv_id_to_node_id_map_dir"] == self.ctx.config["temp_dir"]:
            message = f'You should set your "csv_id_to_node_id_map_dir" config setting to a location other than your system\'s temporary directory ("{self.ctx.config["temp_dir"]}").'
            # print("Warning: " + message)
            self.ctx.log.warning(message)

        if is_running_in_docker():
            docker_message = (
                "It appears you are running Workbench within a Docker container. Please ensure that your CSV ID to node ID map "
                + "config setting defines a location accessible outside of the Docker container; otherwise, it might be deleted when you destroy or rebuild your Docker image."
            )
            print("Warning: " + docker_message)
            self.ctx.log.warning(docker_message)
        if (
            self.ctx.config["recovery_mode_starting_from_node_id"] is not False
            and value_is_numeric(self.ctx.config["recovery_mode_starting_from_node_id"]) is True
        ):
            message = f'"recovery_mode" option in effect. Items that have already been ingested with node IDs starting at {self.ctx.config["recovery_mode_starting_from_node_id"]} will be skipped.'
            print(message)
            self.ctx.log.info(message)

        # Check to see if there are any "host" column values in the CSV ID to node ID map that
        # aren't empty or the current self.ctx.config["host"] value.
        check_for_parent_csv_data = get_csv_data(self.ctx.config)
        check_for_parent_csv_headers = check_for_parent_csv_data.fieldnames
        # This is the set of conditions where the map is queried to get parent node IDs. AFAIK it's
        # complete but if others come up, they should be added here.
        if (
            len(self.ctx.config["csv_id_to_node_id_map_allowed_hosts"]) > 0
            or (os.environ.get("ISLANDORA_WORKBENCH_SECONDARY_TASKS") is not None)
            or (
                "parent_id" in check_for_parent_csv_headers
                and self.ctx.config["query_csv_id_to_node_id_map_for_parents"] is True
            )
            or (
                self.ctx.config["recovery_mode_starting_from_node_id"] is not False
                and value_is_numeric(self.ctx.config["recovery_mode_starting_from_node_id"])
                is True
            )
        ):
            csv_to_node_id_map_path = self.ctx.config["csv_id_to_node_id_map_path"]
            current_host = self.ctx.config["host"]

            prepare_csv_id_to_node_id_map(self.ctx.config)
            check_for_host_column_result = sqlite_manager(
                self.ctx.config,
                operation="select",
                db_file_path=csv_to_node_id_map_path,
                query="select * from pragma_table_info(?)",
                values=("csv_id_to_node_id_map",),
            )
            if check_for_host_column_result[-1][1] == "host":
                num_unique_hosts_result = sqlite_manager(
                    self.ctx.config,
                    operation="select",
                    db_file_path=csv_to_node_id_map_path,
                    query="select distinct host from csv_id_to_node_id_map",
                )

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
                    self.ctx.log.warning(multiple_hosts_in_map_console_message, log_message=multiple_hosts_in_map_log_message)
                else:
                    self.ctx.log.info(
                        'No unexpected values in the CSV ID to node ID map\'s "host" column.'
                    )

        # Check for presence of required config keys, which varies by task.
        if self.ctx.config["nodes_only"] is True:
            self.ctx.log.info('"nodes_only" option in effect. Media files will not be checked/validated.')

        try:
            check_for_required_config_keys(
                self.config_keys, ["task", "host", "username", "password"], throw_exception=True
            )
        except KeyError as e:
            self.ctx.log.error(f"Missing required configuration key: {e}")
            sys.exit(1)  # todo: replace with an exception

        self.ctx.log.info("OK, configuration file has all required values (did not check for optional values).")

        # Check that the rollback configuration file and CSV file directories exist and are writable.
        check_rollback_file_path_directories(self.ctx.config)

        check_csv_file_exists(self.ctx.config, "node_fields")

        # Check column headers in CSV file.
        csv_data = get_csv_data(self.ctx.config)
        if self.ctx.config["csv_headers"] == "labels":
            fieldname_map_cache_path = os.path.join(
                self.ctx.config["temp_dir"], f"node-{self.ctx.config['content_type']}-labels.fieldname_map", )
            if os.path.exists(fieldname_map_cache_path):
                os.remove(fieldname_map_cache_path)
            csv_column_headers = replace_field_labels_with_names(
                self.ctx.config, csv_data.fieldnames
            )
        else:
            csv_column_headers = csv_data.fieldnames

        self.row_column_checks(csv_data, csv_column_headers)
        self.row_filter_checks()

        if self.ctx.config["id_field"] not in csv_column_headers:
            message = f'For "create" tasks, your CSV file must have a column containing a unique identifier (column {self.ctx.config["id_field"]} missing).'
            self.ctx.log.error(message, log_only=True)
            sys.exit("Error: " + message)  # todo: replace with exception

        if "title" not in csv_column_headers:
            message = 'For "create" tasks, your CSV file must contain a "title" column.'
            self.ctx.log.error(message, log_only=True)
            sys.exit("Error: " + message)  # todo: replace with exception

        if (
                self.ctx.config["nodes_only"] is False
                and "file" not in csv_column_headers
                and self.ctx.config["paged_content_from_directories"] is False
                and self.ctx.config["paged_content_from_directories_parents_exist"] is False
        ):
            message = 'For "create" tasks, your CSV file must contain a "file" column.'
            self.ctx.log.error(message, log_only=True)
            sys.exit("Error: " + message)  # todo: replace with exception

        if "output_csv" in self.ctx.config.keys():
            if os.path.exists(self.ctx.config["output_csv"]):
                message = ("Output CSV already exists at " + self.ctx.config[
                    "output_csv"] + ", records will be appended to it.")
                self.ctx.log.info(message)

        if "url_alias" in csv_column_headers:
            validate_url_aliases(self.ctx.config, csv_data)

        # Assumes that only primary tasks have something in their 'secondary_tasks' config setting.
        if self.ctx.config["secondary_tasks"] is not None:
            # We populate the ISLANDORA_WORKBENCH_PRIMARY_TASK_EXECUTION_START_TIME environment variable here so secondary
            # tasks can access it during in validate_parent_ids_in_csv_id_to_node_id_map().
            workbench_execution_start_time = "{:%Y-%m-%d %H:%M:%S}".format(
                datetime.datetime.now()
            )
            os.environ["ISLANDORA_WORKBENCH_PRIMARY_TASK_EXECUTION_START_TIME"] = (workbench_execution_start_time)

        if "parent_id" in csv_column_headers:
            validate_parent_ids_precede_children(
                self.ctx.config, csv_data
            )
            prepare_csv_id_to_node_id_map(self.ctx.config)
            if self.ctx.config["query_csv_id_to_node_id_map_for_parents"] is True:
                validate_parent_ids_in_csv_id_to_node_id_map(
                    self.ctx.config, csv_data
                )
            else:
                self.ctx.log.warning("Only node IDs for parents created during this session will be used (not using the CSV ID to node ID map).")

        # Specific to creating aggregated content such as collections, compound objects and paged content. Currently, if 'parent_id' is present
        # in the CSV file 'field_member_of' is mandatory.
        if "parent_id" in csv_column_headers:
            if "field_weight" not in csv_column_headers:
                self.ctx.log.info('If you are ingesting compound objects, a "field_weight" column is required in your input CSV file.')
            if "field_member_of" not in csv_column_headers:
                message = 'If your CSV file contains a "parent_id" column, it must also contain a "field_member_of" column (with empty values in child rows).'
                self.ctx.log.error(message)
                sys.exit("Error: " + message)  # todo: replace with exception

        field_definitions = get_field_definitions(self.ctx.config, "node")
        drupal_fieldnames = []
        for drupal_fieldname in field_definitions:
            drupal_fieldnames.append(drupal_fieldname)

        if len(drupal_fieldnames) == 0:
            message = "Workbench cannot retrieve field definitions from Drupal. Please confirm that the Field, Field Storage, and Entity Form Display REST resources are enabled."
            self.ctx.log.error(message)
            sys.exit()  # todo: replace with exception

        additional_files_config = get_additional_files_config(self.ctx.config)

        # TODO: It does not make sense to do this but only exit if a configuration is set, but then fail further down.
        #if self.ctx.config["list_missing_drupal_fields"] is True:
        all_fields_check_list = []
        all_fields_check_list.extend(drupal_fieldnames)
        all_fields_check_list.extend(self.node_base_fields)
        all_fields_check_list.extend(self.reserved_fields)
        all_fields_check_list.extend(additional_files_config.keys())
        all_fields_check_list.append(self.ctx.config["id_field"])
        if "ignore_csv_columns" in self.ctx.config.keys():
            all_fields_check_list.extend(self.ctx.config["ignore_csv_columns"])
        # Check for any column headers that do not exist in the above lists or is the configured "id_field"
        missing_drupal_fields = [header for header in csv_column_headers if header not in all_fields_check_list]

        if len(missing_drupal_fields) > 0:
            missing_drupal_fields_message = ", ".join(missing_drupal_fields)
            self.ctx.log.error(
                f"The following header(s) require a matching Drupal field name: {missing_drupal_fields_message}.")
            sys.exit(1)  # todo: replace with exception

        # We .remove() CSV column headers for this check because they are not Drupal field names (including 'langcode').
        csv_column_headers = [header for header in csv_column_headers if header not in self.reserved_fields]

        # langcode is a standard Drupal field but it doesn't show up in any field configs.
        langcode_was_present = False
        if "langcode" in csv_column_headers:
            csv_column_headers.remove("langcode")
            # Set this so we can validate langcode below.
            langcode_was_present = True

        # We .remove() CSV column headers that use the 'media:video:field_foo' media track convention.
        media_track_headers = list()
        for column_header in csv_column_headers:
            if column_header.startswith("media:"):
                media_track_header_parts = column_header.split(":")
                # We also validate the structure of the media track column headers.
                if len(media_track_header_parts) != 3:
                    message = f'"{column_header}" is not a valid media track CSV header.'
                    self.ctx.log.error(message)
                    sys.exit()
                if (
                        media_track_header_parts[1] in self.ctx.config["media_track_file_fields"].keys()
                    and media_track_header_parts[2] == self.ctx.config["media_track_file_fields"][media_track_header_parts[1]]
                ):
                    media_track_headers.append(column_header)
        csv_column_headers = [header for header in csv_column_headers if header not in media_track_headers]

        # Check the configuration that is necessary for verifying nodes already exist in the target Drupal.
        if "node_exists_verification_view_endpoint" in self.ctx.config:
            node_exists_config = get_node_exists_verification_view_endpoint(self.ctx.config)
            if node_exists_config is not False:
                if node_exists_config[0] not in csv_column_headers:
                    message = f'CSV column identified in "node_exists_verification_view_endpoint" is not in your CSV file.'
                    self.ctx.log.error(message)
                    sys.exit()  # todo: replace with exception
                view_url = f'{self.ctx.config["host"]}/{node_exists_config[1].lstrip("/")}'
                view_path_status_code = ping_view_endpoint(self.ctx.config, view_url)
                if view_path_status_code != 200:
                    message = f'Cannot access View REST export configured in "node_exists_verification_view_endpoint" ({view_url}).'
                    self.ctx.log.error(message)
                    sys.exit()  # todo: replace with exception
                else:
                    message = f'View REST export configured in "node_exists_verification_view_endpoint" ({view_url}) is accessible. Values in the "{node_exists_config[0]}" CSV column will be used to check whether nodes already exist.'
                    self.ctx.log.info(message)

        # Check for the View that is necessary for entity reference fields configured
        # as "Views: Filter by an entity reference View" (issue 452).
        for csv_column_header in csv_column_headers:
            if csv_column_header in field_definitions and field_definitions[csv_column_header]["handler"] == "views":
                if (
                        self.ctx.config["require_entity_reference_views"] is True
                        and "entity_reference_view_endpoints" not in self.ctx.config
                ):
                    entity_reference_view_exists = ping_entity_reference_view_endpoint(
                        self.ctx.config, csv_column_header, field_definitions[csv_column_header]["handler_settings"], )
                    if not entity_reference_view_exists:
                        console_message = ('Workbench cannot access the View "' +
                                           field_definitions[csv_column_header]["handler_settings"]["view"][
                                               "view_name"] + '" required to validate values for field "' + csv_column_header + '". See log for more detail.')
                        log_message = ('Workbench cannot access the path defined by the REST Export display "' +
                                       field_definitions[csv_column_header]["handler_settings"]["view"][
                                           "display_name"] + '" in the View "' +
                                       field_definitions[csv_column_header]["handler_settings"]["view"][
                                           "view_name"] + '" required to validate values for field "' + csv_column_header + '". Please check your Drupal Views configuration.' + ' See the "Entity Reference Views fields" section of ' + "https://mjordan.github.io/islandora_workbench_docs/fields/#entity-reference-views-fields for more info.")
                        self.ctx.log.error(console_message, log_message=log_message)
                        sys.exit()  # todo: replace with exception
                else:
                    message = f'Workbench will not validate values in your CSV file\'s "{csv_column_header}" column because your "require_entity_reference_views" configuration setting is "false".'
                    self.ctx.log.warning(message, log_message=message + ' See the "Entity Reference Views fields" section of ' + "https://mjordan.github.io/islandora_workbench_docs/fields/#entity-reference-views-fields for more info.")

        self.ctx.log.info("OK, CSV column headers match Drupal field names.")

        if "field_viewer_override_extensions" in self.ctx.config or "field_viewer_override_models" in self.ctx.config:
            preprocessed_input_csv_file_path = get_preprocessed_input_csv_file_path(self.ctx.config)
            message = f'You should review "{preprocessed_input_csv_file_path}" to ensure that values in the "field_viewer_override" column have been correctly assigned based on your configuration settings.'
            self.ctx.log.warning(message)

        required_drupal_fields_node = get_required_bundle_fields(
            self.ctx.config, "node", self.ctx.config["content_type"]
        )
        for required_drupal_field in required_drupal_fields_node:
            if (required_drupal_field == "field_model" and self.ctx.config[
                "paged_content_from_directories_parents_exist"] and "paged_content_page_model_tid" in self.ctx.config):
                continue
            if required_drupal_field not in csv_column_headers:
                self.ctx.log.error("Required Drupal field %s is not present in the CSV file.", required_drupal_field)
                sys.exit()  # todo: replace with exception
        self.ctx.log.info("OK, required Drupal fields are present in the CSV file.")

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_required_fields_have_values(
            self.ctx.config, required_drupal_fields_node, csv_data, )

        # Validate dates in 'created' field, if present.
        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        if "created" in csv_column_headers:
            csv_data = get_csv_data(self.ctx.config)
            validate_node_created_date(self.ctx.config, csv_data)
        # Validate user IDs in 'uid' field, if present.
        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        if "uid" in csv_column_headers:
            csv_data = get_csv_data(self.ctx.config)
            validate_node_uid(self.ctx.config, csv_data)

        if not self.ctx.config["nodes_only"]:
            # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
            validate_media_use_tid(self.ctx.config)
            # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
            csv_data = get_csv_data(self.ctx.config)
            validate_media_use_tids_in_csv(self.ctx.config, csv_data)

            if self.ctx.config["fixity_algorithm"] is not None:
                allowed_algorithms = ["md5", "sha1", "sha256"]
                if self.ctx.config["fixity_algorithm"] not in allowed_algorithms:
                    message = ("Configured fixity algorithm '" + self.ctx.config[
                        "fixity_algorithm"] + "' must be one of 'md5', 'sha1', or 'sha256'.")
                    self.ctx.log.error(message)
                    sys.exit(1)  # todo: replace with exception

            if self.ctx.config["validate_fixity_during_check"] and self.ctx.config["fixity_algorithm"] is not None:
                field_and_checksum_in_csv = False
                fixity_message = "Performing local checksum validation."
                self.ctx.log.info(fixity_message + " This might take some time.", log_message=fixity_message)

                if self.ctx.config["task"] == "add_media":
                    row_id = "node_id"
                else:
                    row_id = self.ctx.config["id_field"]
                checksum_validation_all_ok = True

                for checksum_validation_row_count, checksum_validation_row in enumerate(
                        csv_data, start=1
                ):
                    file_name = checksum_validation_row["file"]
                    if "file" in checksum_validation_row and "checksum" in checksum_validation_row:
                        field_and_checksum_in_csv = True
                        if (
                                file_name.lower().startswith("http") is False
                                and check_file_exists(self.ctx.config, file_name) is True
                        ):
                            if os.path.isabs(file_name):
                                file_path = file_name
                            else:
                                file_path = os.path.join(self.ctx.config["input_dir"], file_name)
                            hash_from_local = get_file_hash_from_local(
                                self.ctx.config, file_path, self.ctx.config["fixity_algorithm"]
                            )
                            if hash_from_local is False:
                                continue
                            if hash_from_local == checksum_validation_row["checksum"].strip():
                                self.ctx.log.info(
                                    f'Local {self.ctx.config["fixity_algorithm"]} checksum and value in the CSV "checksum" field for file "{file_path}" ({hash_from_local}) match.', log_only=True
                                )
                            else:
                                checksum_validation_all_ok = False
                                self.ctx.log.warning(
                                    f'Local {self.ctx.config["fixity_algorithm"]} checksum and value in the CSV "checksum" field for file "{file_path}" (named in CSV row "{checksum_validation_row[row_id]}") do not match (local: {hash_from_local}, CSV: {checksum_validation_row["checksum"]}).', log_only=True
                                )

                if field_and_checksum_in_csv is True:
                    if checksum_validation_all_ok is True:
                        self.ctx.log.info(
                            "OK, checksum validation during complete. All checks pass. See the log for more detail.",
                            log_message="OK, checksum validation during complete. All checks pass."
                        )
                    else:
                        self.ctx.log.warning(
                            f"Warning: Not all checksum validation passed. See the log for more detail.",
                            log_message="Not all checksum validation passed."
                        )
                else:
                    checksum_validation_message = 'Could not validate checksums because the input CSV did not contain both "field" and "checksum" columns.'
                    self.ctx.log.warning(checksum_validation_message)

        row_counter = 0
        for count, row in enumerate(csv_data, start=1):
            row_counter += 1
            if "image_alt_text" in row:
                if len(row["image_alt_text"]) > self.ctx.config["max_image_alt_text_length"]:
                    image_alt_text = row["image_alt_text"]
                    max_alt_text_length = self.ctx.config["max_image_alt_text_length"]
                    node_id = row[self.ctx.config["id_field"]]
                    message = f"Alt text in input CSV row with node ID {node_id} is longer than the maximum configured alt text length ({max_alt_text_length})"
                    self.ctx.log.warning(
                        message + ". See log for more information.", log_message=
                        message + f" (length is {len(image_alt_text)} characters). Adding the alt text in this row will be skipped."
                    )

        # todo: validate the below by passing the row instead of having each validate iterate over the entire csv file.

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_geolocation_fields(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_link_fields(self.ctx.config, field_definitions, csv_data)

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_authority_link_fields(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_edtf_fields(self.ctx.config, field_definitions, csv_data)

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_csv_field_cardinality(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_csv_field_length(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_text_list_fields(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        warn_user_about_taxo_terms = validate_taxonomy_field_values(
            self.ctx.config, field_definitions, csv_data
        )
        if warn_user_about_taxo_terms is True:
            self.ctx.log.warning(
                "Issues detected with validating taxonomy field values in the CSV file. See the log for more detail."
            )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        warn_user_about_typed_relation_terms = validate_typed_relation_field_values(
            self.ctx.config, field_definitions, csv_data
        )
        if warn_user_about_typed_relation_terms is True:
            self.ctx.log.warning(
                "Warning: Issues detected with validating typed relation field values in the CSV file. See the log for more detail."
            )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_numeric_fields(self.ctx.config, field_definitions, csv_data)

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        csv_data = get_csv_data(self.ctx.config)
        validate_media_track_fields(self.ctx.config, csv_data)

        # Validate existence of nodes specified in 'field_member_of'. This could be generalized out to validate node IDs in other fields.
        # See https://github.com/mjordan/islandora_workbench/issues/90.
        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        if self.ctx.config["validate_parent_node_exists"] is True:
            for count, row in enumerate(csv_data, start=1):
                if "field_member_of" in csv_column_headers:
                    parent_nids = row["field_member_of"].split(self.ctx.config["subdelimiter"])
                    for parent_nid in parent_nids:
                        if len(parent_nid) > 0:
                            if not ping_node(
                                self.ctx.config, parent_nid, warn=False
                            ):
                                log_message = ("The 'field_member_of' field in row with ID '" + row[self.ctx.config[
                                    "id_field"]] + "' of your CSV file contains a node ID (" + parent_nid + ") that " + "doesn't exist or is not accessible. See the workbench log for more information.")
                                message = f'Node identified in "field_member_of" ({parent_nid}) in row with ID "{row[self.ctx.config["id_field"]]}" cannot be found or accessed.'
                                self.ctx.log.error(message, log_message=log_message)
                                sys.exit(1)  # todo: replace with exception
        else:
            message = (
                    '"validate_parent_node_exists" is set to false. Node IDs in "field_member_of" that do not exist or are not accessible ' + 'will result in 422 errors in "create" and "update" tasks.')
            self.ctx.log.warning(message, log_only=True)

        # Check the configuration that is necessary for enabling use of term names in Entity Reference Views fields.
        if "entity_reference_view_endpoints" in self.ctx.config:
            entity_reference_view_endpoints = get_entity_reference_view_endpoints(
                self.ctx.config
            )
            for entity_reference_view_field_name, entity_reference_view_endpoint in entity_reference_view_endpoints.items():
                if entity_reference_view_field_name not in csv_column_headers:
                    message = f'CSV column {entity_reference_view_field_name} identified in "entity_reference_view_endpoints" is not in your CSV file.'
                    self.ctx.log.error(message)
                    sys.exit(1)  # todo: replace with exception
                view_url = f'{self.ctx.config["host"]}/{entity_reference_view_endpoint.lstrip("/")}'
                view_path_status_code = ping_view_endpoint(self.ctx.config, view_url)
                if view_path_status_code != 200:
                    message = f'Cannot access View REST export configured in "entity_reference_view_endpoints" ({view_url}).'
                    self.ctx.log.error(message)
                    sys.exit(1)  # todo: replace with exception
                else:
                    message = f'View REST export configured in "entity_reference_view_endpoints" ({view_url}) is accessible.'
                    self.ctx.log.info(message)

        # Validate 'langcode' values if that field exists in the CSV.
        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        if langcode_was_present:
            validate_langcode_csv_data = get_csv_data(self.ctx.config)
            for count, row in enumerate(validate_langcode_csv_data, start=1):
                langcode_valid = validate_language_code(row["langcode"])
                if not langcode_valid:
                    message = ("Row with ID " + row[
                        self.ctx.config["id_field"]] + " of your CSV file contains an invalid Drupal language code (" + row[
                                   "langcode"] + ") in its 'langcode' column.")
                    self.ctx.log.error(message)
                    sys.exit(1)  # todo: replace with exception

        self.postprocess_script_checks()

    def prepare(self):
        # Prepare for the create task (e.g., validate input, set up environment)
        pass

    def rows(self):
        # Yield rows of data to be processed for the create task
        pass

    def process_row(self, row):
        # Implement the logic to process each row for the create task
        pass

    def finish(self):
        # Finalize the create task (e.g., summarize results, clean up)
        pass