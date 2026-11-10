import copy
import json
from typing import Union

from islandora_workbench.drupal.fields import get_required_bundle_fields, WorkbenchFieldFactory, get_field_definitions
from islandora_workbench.exceptions import WorkbenchValidationException
from islandora_workbench.tasks import register_task
from islandora_workbench.tasks.base import Task
from islandora_workbench.utils import value_is_numeric
from workbench_utils import get_additional_files_config, patch_revision_log_message
from islandora_workbench.external_scripts import execute_entity_post_task_script
from islandora_workbench.data_io.csv.csv_process import preprocessor_scripts_csv_row
from islandora_workbench.drupal.client import ping_node, get_entity_fields, is_revisions_enabled, update_node, \
    create_url_alias
from islandora_workbench.drupal.utils import validate_language_code, get_nid_from_url_alias
from islandora_workbench.checks.validation import validate_url_aliases_action, \
    validate_geolocation_fields_action, validate_input_dir, validate_node_created_date_action, \
    validate_link_fields_action, validate_authority_link_value_action, validate_taxonomy_reference_value_action, \
    validate_typed_relation_field_value_action, validate_media_track_value, validate_entity_reference_view_endpoints


@register_task("update")
class UpdateTask(Task):

    name = "update"
    required_fields = ("node_id", )
    required_config_keys = ("host", "id_field", "update_mode", "subdelimiter", "validate_parent_node_exists")

    def __init__(self, ctx: dict):
        super().__init__(ctx)
        self.field_configs["entity_fields"] = list(
            get_entity_fields(self.ctx['config'], "node", self.ctx['config']["content_type"]).keys()
        )
        self.field_configs["entity_field_definitions"] = get_field_definitions(self.ctx["config"], "node")
        if self.ctx['config']["id_field"] not in self.field_configs["entity_fields"]:
            self.field_configs["entity_fields"].append(self.ctx['config']["id_field"])

        if "additional_files" in self.ctx["config"] and len(self.ctx["config"]["additional_files"]) > 0:
            self.field_configs["additional_files"] = self.ctx["config"]["additional_files"]
        else:
            self.field_configs["additional_files"] = {}
        self.field_configs["required_bundle_fields"] = get_required_bundle_fields(
            self.ctx["config"], "node", self.ctx["config"]["content_type"]
        )

    def general_checks(self):
        update_mode_options = ["replace", "append", "delete"]
        if self.ctx["config"]["update_mode"] not in update_mode_options:
            message = ('Your "update_mode" config option must be one of the following: ' + ", ".join(
                update_mode_options
            ) + ".")
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

        validate_input_dir(self.ctx["config"])

        if "file" in self.ctx["csv"].field_names:
            message = 'Error: CSV column header "file" is not allowed in update tasks.'
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

        remove_fields = ["title", "url_alias", "image_alt_text", "media_use_tid", "revision_log", "node_id"]
        csv_column_headers = [header for header in self.ctx["csv"].field_names if header not in remove_fields]

        # langcode is a standard Drupal field but it doesn't show up in any field configs.
        # Set this so we can validate langcode below in check_row.
        self.testing_flags["langcode_was_present"] = "langcode" in csv_column_headers
        if "langcode" in csv_column_headers:
            csv_column_headers.remove("langcode")

        all_fields_check_list = []
        all_fields_check_list.extend(self.field_configs["entity_field_definitions"].keys())
        all_fields_check_list.extend(self.NODE_BASE_FIELDS)

        # Check for any column headers that do not exist in the above lists or is the configured "id_field"
        missing_drupal_fields = [header for header in csv_column_headers if header not in all_fields_check_list]

        if len(missing_drupal_fields) > 0:
            missing_drupal_fields_message = ", ".join(missing_drupal_fields)
            self.ctx["log"].error(
                f"The following header(s) require a matching Drupal field name: {missing_drupal_fields_message}."
            )
            raise WorkbenchValidationException(
                f"The following header(s) require a matching Drupal field name: {missing_drupal_fields_message}."
                )

        self.ctx["log"].info("OK, CSV column headers match Drupal field names.")

        validate_entity_reference_view_endpoints(self.ctx, csv_column_headers)

    def check_row(self, row: Union[dict, str], row_number: int):
        csv_column_headers = copy.copy(self.ctx["csv"].field_names)
        row_id = row[self.id_field]
        row_level_errors = []
        row_level_warnings = []

        if "url_alias" in csv_column_headers:
            try:
                validate_url_aliases_action(self.ctx["config"], row)
            except WorkbenchValidationException as e:
                self.ctx["log"].error(f"Row {row_number}: {str(e)}", log_only=True)
                row_level_errors.append(f"Row {row_number}: {str(e)}")

        # Validate geolocation fields
        for field_name in self.field_types["geolocation"]:
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_geolocation_fields_action(self.ctx["config"], row, field_name)
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(f"Row {row_number}: {str(e)}", log_only=True)
                    row_level_errors.append(f"Row {row_number}: {str(e)}")

        # Validate link fields
        for field_name in self.field_types["link"]:
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_link_fields_action(self.ctx["config"], row, field_name)
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(f"Row {row_number}: {str(e)}", log_only=True)
                    row_level_errors.append(f"Row {row_number}: {str(e)}")

        # Validate authority link fields
        for field_name in self.field_types["authority_link"]:
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_authority_link_value_action(
                        self.ctx["config"],
                        self.field_configs["entity_field_definitions"][field_name]["authority_sources"],
                        row,
                        field_name
                    )
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(f"Row {row_number}: {str(e)}", log_only=True)
                    row_level_errors.append(f"Row {row_number}: {str(e)}")

        # Validate dates in any other fields that are of type 'edtf'.
        for field_name in self.field_types["edtf"]:
            if field_name in row and row[field_name].strip() != "":
                try:
                    validate_node_created_date_action(self.ctx["config"], row, field_name)
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(f"Row {row_number}: {str(e)}", log_only=True)
                    row_level_errors.append(f"Row {row_number}: {str(e)}")

        # Taxonomy term references
        for field_name in self.field_types["taxonomy_term_reference"]:
            if field_name in row and row[field_name].strip() != "" and "vocabularies" in \
                    self.field_configs["entity_field_definitions"][field_name]:
                try:
                    validate_taxonomy_reference_value_action(
                        self.ctx["config"], self.field_configs["entity_field_definitions"], field_name,
                        row[field_name], row_number
                        )
                except WorkbenchValidationException as e:
                    self.ctx["log"].warning(f"Row {row_number}: {str(e)}", log_only=True)
                    row_level_warnings.append(f"Row {row_number}: {str(e)}")

        # Cardinality
        for field_name in self.field_types["cardinality"]:
            if field_name in row and row[field_name].strip() != "":
                cardinality_value = self.field_configs["entity_field_definitions"][field_name]["cardinality"]
                delimited_field_values = row[field_name].split(
                    self.ctx["config"]["subdelimiter"]
                )
                if len(delimited_field_values) > cardinality_value:
                    message = (
                            f'Row {row_number}: CSV field "{field_name}" in record with ID {row_id} contains more values ' + f'than the number of values allowed for that field (max {cardinality_value}). Workbench with add only the first{"" if cardinality_value == 1 else f" {cardinality_value}"} value(s) for that field.')
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
                        ) + " characters). Workbench will truncate this value prior to populating Drupal.")
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
                        self.ctx["config"], self.field_configs["entity_field_definitions"], field_name,
                        row[field_name], row_number
                    )
                except WorkbenchValidationException as e:
                    self.ctx["log"].error(f"Row {row_number}: {str(e)}", log_only=True)
                    row_level_errors.append(f"Row {row_number}: {str(e)}")

        # Validate media track fields
        for field_name, media_bundle_name in self.field_types["media_fields"].items():
            if field_name in self.field_configs["media_bundle_field_definitions"][media_bundle_name]:
                if self.field_configs["media_bundle_field_definitions"][media_bundle_name][field_name][
                    "field_type"] == "media_track":
                    field_values = row[field_name].split(self.ctx["config"]["subdelimiter"])
                    for field_value in field_values:
                        if len(field_value.strip()) > 0:
                            if not validate_media_track_value(field_value):
                                message = (
                                    f'Value in field "{field_name}" in row with ID "{row_id}" ({field_value}) has a media type is not a valid media track field value.')
                                self.ctx["log"].error(message, log_only=True)
                                row_level_errors.append(message)
            else:
                message = (
                    f'Field "{field_name}" in row with ID "{row_id}" is not defined in the media bundle "{media_bundle_name}".')
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
                        log_message = ("The 'field_member_of' field in row with ID '" + row[
                            self.ctx["config"][
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

        if len(row_level_errors) > 0:
            self.ctx["log"].error(f"Row {row_number} has validation errors:", print_only=True)
            for error in row_level_errors:
                self.ctx["log"].error(error, print_only=True)
            raise WorkbenchValidationException(f"Row {row_number} has validation errors")
        if len(row_level_warnings) > 0:
            self.ctx["log"].warning(f"Row {row_number} has validation warnings:", print_only=True)
            for warning in row_level_warnings:
                self.ctx["log"].warning(warning, print_only=True)

    def prepare_run(self):
        if self.ctx["config"]["log_term_creation"] is False:
            self.ctx["log"].info(
                "'log_term_creation' configuration setting is False. Creation of new taxonomy terms will not be logged."
            )

    def process_row(self, row: Union[dict, str], row_number: int):

        node_id_to_ping = copy.copy(row["node_id"])
        node_ping_result = ping_node(self.ctx["config"], node_id_to_ping, "GET", True)
        if node_ping_result is False:
            if self.ctx["config"]["progress_bar"] is False:
                self.ctx["log"].warning(
                    f"Node {node_id_to_ping} not found or not accessible, skipping update."
                )
            return

        # Add the target_id field.
        node = {"type": [{"target_id": self.ctx["config"]["content_type"]}]}

        node_field_values = json.loads(node_ping_result)

        # Some optional node base fields.
        optional_fields = {
            "uid": "target_id",
            "created": "value",
            "langcode": "value",
            "status": "value",
            "promote": "value",
        }
        for field, output_key in optional_fields.items():
            if field in self.ctx["csv"].field_names and len(row[field]) > 0:
                node[field] = [{output_key: row[field]}]

        required_fields = ["node_id"]
        custom_fields = list(set(self.ctx["csv"].field_names) - set(required_fields))

        node_has_all_fields = True
        for custom_field in custom_fields:
            # If node doesn't have the field, log that fact and skip updating the field.
            reserved_fields = ["published", "url_alias"]
            if (
                custom_field not in node_field_values.keys()
                and custom_field not in reserved_fields
            ):
                message = f'Node {row["node_id"]} does not have a "{custom_field}" field, skipping update.'
                self.ctx["log"].warning(message)
                node_has_all_fields = False
                break

            # If a field is registered in the config to have a preprocessor applied to it
            # (including empty fields), apply the processor.
            if "preprocessors" in self.ctx["config"] and custom_field in self.ctx["config"]["preprocessors"]:
                row[custom_field] = preprocessor_scripts_csv_row(self.ctx["config"], row, custom_field, self.ctx["config"]["preprocessors"][custom_field])

            # Skip updating field if CSV field is empty (other than for 'delete' update mode).
            # For 'delete' update mode it doesn't matter if there's anything in the CSV field,
            # but users expect to be able to supply empty values for this operation.
            if len(row[custom_field].strip()) == 0 and self.ctx["config"]["update_mode"] != "delete":
                continue

            # 'url_alias' is a reserved CSV field.
            # 'image_alt_text' is a reserved CSV field.
            # Issue to add alt text in update task is https://github.com/mjordan/islandora_workbench/issues/166.
            # 'langcode' is a core Drupal field, but is not considered a base field.
            # 'created' is a base field.
            # 'published' is a reserved CSV field.
            # 'uid' is a base field.
            # 'promote' is a base field.
            base_fields = [
                "url_alias",
                "image_alt_text",
                "langcode",
                "created",
                "published",
                "uid",
                "promote",
                "revision_log",
            ]
            if custom_field in base_fields:
                continue


            # Assemble Drupal field structures from CSV data. If new field types are added to
            # fields.py, they need to be registered in the following if/elif/else block.
            field = WorkbenchFieldFactory.get_field_handler(
                self.field_configs["entity_field_definitions"][custom_field]["field_type"]
            )
            node = field.update(
                self.ctx["config"],
                self.field_configs["entity_field_definitions"],
                node,
                row,
                custom_field,
                node_field_values[custom_field],
            )

        if node_has_all_fields is True:
            if not value_is_numeric(row["node_id"]):
                row["node_id"] = get_nid_from_url_alias(self.ctx["config"], row["node_id"])

            node_headers = {"Content-Type": "application/json"}
            if "revision_log" in row.keys() and is_revisions_enabled(
                self.ctx["config"], "node", self.ctx["config"]["content_type"]
            ):
                node.update(patch_revision_log_message("node", row["revision_log"]))

            node_response = update_node(self.ctx, row["node_id"], node)
            if node_response["status_code"] == 200:
                row_position_message = ""
                if self.ctx["config"]["show_percentage_of_csv_input_processed"] is True:
                    row_position = int(row_number) / int(self.ctx["csv"].num_records) * 100
                    row_position_message = f" ({int(row_position)}%)"
                self.ctx["log"].info(
                    f'Node { self.ctx["config"]["host"]}/node/{row["node_id"]} updated{row_position_message}.',
                    log_only=self.ctx["config"]["progress_bar"]
                )

            else:
                # This message only appears if you are not using the progress bar.
                self.ctx["log"].error(
                    f"node {self.ctx['config']['host']}/node/{row['node_id']} not updated. See log for more detail.",
                    log_only=self.ctx["config"]["progress_bar"]
                )
                self.ctx["log"].error(
                    f"Node {self.ctx['config']['host']}/node/{row['node_id']} not updated (server response code was '{node_response['status_code']}').",
                    log_only=True
                )
                self.ctx["log"].error(
                    f'JSON request body used in previous PATCH to "{self.ctx["config"]["host"]}/node/{row["node_id"]}" was {node}.',
                    log_only=True
                )

            # Execute node-specific post-create scripts, if any are configured.
            if "node_post_update" in self.config_keys and len(self.ctx["config"]["node_post_update"]) > 0:
                for command in self.ctx["config"]["node_post_update"]:
                    (
                        post_task_output,
                        post_task_return_code,
                    ) = execute_entity_post_task_script(
                        command,
                        self.ctx["config"]["config_file"],
                        node_response["status_code"],
                        node_response["response_body"],
                    )
                    if post_task_return_code == 0:
                        self.ctx["log"].info(
                            f"Post node update script {command} executed successfully."
                        )
                    else:
                        self.ctx["log"].error(f"Post node update script {command} failed.")

            if self.ctx["config"]["progress_bar"] is True:
                self.ctx["progress_bar"].updateAmount(row_number)

            # If 'url_alias' is in the CSV, create the alias.
            if "url_alias" in row and len(row["url_alias"]) > 0:
                create_url_alias(self.ctx["config"], row["node_id"], row["url_alias"])
