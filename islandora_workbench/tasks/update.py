import os
import sys

from islandora_workbench.tasks import register_task
from islandora_workbench.tasks.base import Task
from workbench_utils import check_for_required_config_keys, get_csv_data, check_csv_file_exists, \
    replace_field_labels_with_names, validate_url_aliases, get_field_definitions, validate_geolocation_fields, \
    validate_link_fields, validate_authority_link_fields, validate_edtf_fields, validate_csv_field_cardinality, \
    validate_csv_field_length, validate_text_list_fields, validate_taxonomy_field_values, \
    validate_typed_relation_field_values, validate_numeric_fields, validate_media_track_fields, ping_node, \
    get_entity_reference_view_endpoints, ping_view_endpoint, validate_language_code

@register_task("update")
class UpdateTask(Task):
    def rows(self):
        pass

    def process_row(self, row):
        pass

    def __init__(self, ctx: dict):
        super().__init__(ctx)
        self.ctx = ctx

    def check(self):
        super().check()

        try:
            check_for_required_config_keys(
                self.config_keys, ["task", "host", "username", "password"], throw_exception=True
            )
        except KeyError as e:
            self.ctx.log.error(f"Missing required configuration key: {e}")
            sys.exit(1)  # todo: replace with an exception

        update_mode_options = ["replace", "append", "delete"]
        if self.ctx.config["update_mode"] not in update_mode_options:
            message = ('Your "update_mode" config option must be one of the following: ' + ", ".join(
                update_mode_options
                ) + ".")
            self.ctx.log.error(message)
            sys.exit(1)  # todo: replace with an exception

        message = "OK, configuration file has all required values (did not check for optional values)."
        self.ctx.log.info(message)

        check_csv_file_exists(self.ctx.config, "node_fields")

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

        if "file" in csv_column_headers:
            self.ctx.log.error('Error: CSV column header "file" is not allowed in update tasks.')
            sys.exit(1)  # todo: replace with an exception

        self.row_column_checks(csv_data, csv_column_headers)
        self.row_filter_checks()

        if "node_id" not in csv_column_headers:
            self.ctx.log.error('For "update" tasks, your CSV file must contain a "node_id" column.')
            sys.exit(1)  # todo: replace with an exception
        if "url_alias" in csv_column_headers:
            # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
            validate_url_aliases(self.ctx.config, csv_data)

        field_definitions = get_field_definitions(self.ctx.config, "node")

        drupal_fieldnames = field_definitions.keys()
        remove_fields = ["title", "url_alias", "image_alt_text", "media_use_tid", "revision_log", "node_id"]
        csv_column_headers = [header for header in csv_column_headers if header not in remove_fields]

        langcode_was_present = False
        # langcode is a standard Drupal field but it doesn't show up in any field configs.
        if "langcode" in csv_column_headers:
            csv_column_headers.remove("langcode")
            # Set this so we can validate langcode below.
            langcode_was_present = True

        all_fields_check_list = []
        all_fields_check_list.extend(drupal_fieldnames)
        all_fields_check_list.extend(self.node_base_fields)
        if "ignore_csv_columns" in self.ctx.config.keys():
            all_fields_check_list.extend(self.ctx.config["ignore_csv_columns"])

        # Check for any column headers that do not exist in the above lists or is the configured "id_field"
        missing_drupal_fields = [header for header in csv_column_headers if header not in all_fields_check_list]

        if len(missing_drupal_fields) > 0:
            missing_drupal_fields_message = ", ".join(missing_drupal_fields)
            self.ctx.log.error(
                f"The following header(s) require a matching Drupal field name: {missing_drupal_fields_message}.")
            sys.exit(1)  # todo: replace with exception

        self.ctx.log.info("OK, CSV column headers match Drupal field names.")

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_geolocation_fields(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_link_fields(self.ctx.config, field_definitions, csv_data)

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_authority_link_fields(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_edtf_fields(self.ctx.config, field_definitions, csv_data)

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_csv_field_cardinality(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_csv_field_length(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_text_list_fields(
            self.ctx.config, field_definitions, csv_data
        )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        warn_user_about_taxo_terms = validate_taxonomy_field_values(
            self.ctx.config, field_definitions, csv_data
        )
        if warn_user_about_taxo_terms is True:
            self.ctx.log.warning(
                "Issues detected with validating taxonomy field values in the CSV file. See the log for more detail."
            )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        warn_user_about_typed_relation_terms = validate_typed_relation_field_values(
            self.ctx.config, field_definitions, csv_data
        )
        if warn_user_about_typed_relation_terms is True:
            self.ctx.log.warning(
                "Warning: Issues detected with validating typed relation field values in the CSV file. See the log for more detail."
            )

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        validate_numeric_fields(self.ctx.config, field_definitions, csv_data)

        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
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
                            parent_node_exists = ping_node(
                                self.ctx.config, parent_nid, warn=False
                            )
                            if parent_node_exists is False:
                                message = ("The 'field_member_of' field in row with ID '" + row[self.ctx.config[
                                    "id_field"]] + "' of your CSV file contains a node ID (" + parent_nid + ") that " + "doesn't exist or is not accessible. See the workbench log for more information.")
                                message = f'Node identified in "field_member_of" ({parent_nid}) in row with ID "{row[self.ctx.config["id_field"]]}" cannot be found or accessed.'
                                self.ctx.log.error(message)
                                sys.exit(1)  # todo: replace with an exception
        else:
            message = (
                    '"validate_parent_node_exists" is set to false. Node IDs in "field_member_of" that do not exist or are not accessible ' + 'will result in 422 errors in "create" and "update" tasks.')
            self.ctx.log.warning(message)

        # Check the configuration that is necessary for enabling use of term names in Entity Reference Views fields.
        if "entity_reference_view_endpoints" in self.ctx.config:
            entity_reference_view_endpoints = get_entity_reference_view_endpoints(
                self.ctx.config
            )
            for (entity_reference_view_field_name,
                 entity_reference_view_endpoint,) in entity_reference_view_endpoints.items():
                if entity_reference_view_field_name not in csv_column_headers:
                    message = f'CSV column {entity_reference_view_field_name} identified in "entity_reference_view_endpoints" is not in your CSV file.'
                    self.ctx.log.error(message)
                    sys.exit(1)  # todo: replace with an exception
                view_url = (f'{self.ctx.config["host"]}/{entity_reference_view_endpoint.lstrip("/")}')
                view_path_status_code = ping_view_endpoint(self.ctx.config, view_url)
                if view_path_status_code != 200:
                    self.ctx.log.error(f'Cannot access View REST export configured in "entity_reference_view_endpoints" ({view_url}).')
                    sys.exit(1)  # todo: replace with an exception
                else:
                    self.ctx.log.info(f'View REST export configured in "entity_reference_view_endpoints" ({view_url}) is accessible.')

        # Validate 'langcode' values if that field exists in the CSV.
        # @todo: add the 'rows_with_missing_files' method of accumulating invalid values (issue 268).
        if langcode_was_present:
            for count, row in enumerate(csv_data, start=1):
                langcode_valid = validate_language_code(row["langcode"])
                if not langcode_valid:
                    message = ("Row with ID " + row[
                        self.ctx.config["id_field"]] + " of your CSV file contains an invalid Drupal language code (" + row[
                                   "langcode"] + ") in its 'langcode' column.")
                    self.ctx.log.error(message)
                    sys.exit(1)  # todo: replace with an exception

        self.postprocess_script_checks()


    def finish(self):
        # Add any finalization steps specific to the UpdateTask here
        pass

    def run(self):
        # Add any additional steps specific to the UpdateTask here
        pass
