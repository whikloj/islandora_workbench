from pathlib import Path
from typing import Union

from islandora_workbench.checks import validation
from islandora_workbench.drupal.client import ping_node
from islandora_workbench.drupal.media import create_media
from islandora_workbench.drupal.node import get_nid_from_url_alias
from islandora_workbench.exceptions import WorkbenchValidationException, WorkbenchValidationWarning, WorkbenchException
from islandora_workbench.tasks import Task, register_task
from islandora_workbench.tasks.base import validate_media_type_and_extensions
from islandora_workbench.utils import get_file_hash_from_local, check_file_exists, value_is_numeric


@register_task("add_media")
class AddMediaTask(Task):

    name = "add_media"
    required_config_keys = ("host", "username", "password", "media_type")
    required_fields = ("node_id", "file")
    id_field = "node_id"

    def __init__(self, ctx: dict):
        super().__init__(ctx)
        if self.ctx["config"]["allow_missing_files"] is True:
            self.ctx["log"].warning(
                "Configuration setting 'allow_missing_files' is set to 'true'. This may result in media being created without files."
            )
            self.required_fields = tuple("node_id")
        self.field_configs["additional_files"] = self.ctx["config"].get("additional_files", {})

    def general_checks(self):
        csv_column_headers = self.ctx["csv"].field_names

        if self.ctx["config"]["nodes_only"] is False and self.ctx["config"]["keep_filename_parent_directory"] is False:
            self.ctx["log"].info(
                "Parent directory names for files will not be used when creating Drupal media, but the directory names will appear in Workbench messages and log entries."
            )

        if self.ctx["config"]["fixity_algorithm"] is not None:
            allowed_algorithms = ["md5", "sha1", "sha256"]
            if self.ctx["config"]["fixity_algorithm"] not in allowed_algorithms:
                message = ("Configured fixity algorithm '" + self.ctx["config"][
                    "fixity_algorithm"] + "' must be one of 'md5', 'sha1', or 'sha256'.")
                self.ctx["log"].error(message)
                raise WorkbenchValidationException(message)

        validation.validate_media_use_tid(self.ctx["config"])

        self.testing_flags["rows_with_missing_files"] = False
        self.testing_flags["missing_additional_files"] = False

        if not self.ctx["config"]["nodes_only"] and not (self.ctx["config"]["paged_content_from_directories"] or self.ctx["config"]["paged_content_from_directories_parents_exist"]):
            if "additional_files" in self.config_keys and len(self.ctx["config"]["additional_files"]) > 0:
                for additional_file_field, media_use_tid in self.field_configs["additional_files"].items():
                    if additional_file_field not in csv_column_headers:
                        message = (
                                'CSV column "' + additional_file_field + '" registered in the "additional_files" configuration setting is missing from your CSV file.')
                        self.ctx["log"].error(message)
                        raise WorkbenchValidationException(message)
                    try:
                        validation.validate_media_use_tid_in_additional_files_setting(self.ctx["config"], media_use_tid, additional_file_field)
                    except WorkbenchValidationWarning as e:
                        self.ctx["log"].warning(str(e))



    def check_row(self, row: Union[dict, str], row_number: int):
        if "media_use_tid" in row:
            delimited_field_values = row["media_use_tid"].split(
                self.ctx["config"]["subdelimiter"]
            )
            for field_value in delimited_field_values:
                if len(field_value.strip()) > 0:
                    validation.validate_media_use_tid(
                        self.ctx["config"], field_value, row[self.id_field]
                    )

        if not self.ctx["config"]["allow_missing_files"] and "file" in row:
            file_check = row["file"].strip()
            # Check for and log empty 'file' values.
            if len(file_check) == 0:
                message = f"CSV row with ID {row[self.id_field]} contains an empty \"file\" value."
                self.ctx["log"].warning(message)

        if "file" in row and self.ctx["config"]["validate_fixity_during_check"] and self.ctx["config"]["fixity_algorithm"] is not None:
            file_and_checksum_in_csv = "checksum" in row
            if file_and_checksum_in_csv:
                file_name = row["file"]
                if not file_name.lower().startswith("http"):
                    filepath = Path(file_name)
                    if not filepath.is_absolute():
                        filepath = Path(self.ctx["config"]["input_dir"]) / filepath
                    hash_from_local = get_file_hash_from_local(str(filepath), self.ctx["config"]["fixity_algorithm"])
                    if hash_from_local is False:
                        return

                    if hash_from_local == row["checksum"].strip():
                        self.ctx["log"].info(
                            f'Local {self.ctx["config"]["fixity_algorithm"]} checksum and value in the CSV "checksum" field for file "{filepath}" ({hash_from_local}) match.'
                        )
                    else:
                        self.ctx["log"].warning(
                            f'Local {self.ctx["config"]["fixity_algorithm"]} checksum and value in the CSV "checksum" field for file "{filepath}" (named in CSV row "{row[self.ctx["config"]["id_field"]]}") do not match (local: {hash_from_local}, CSV: {row["checksum"]}).'
                        )
            else:
                self.ctx["log"].warning(
                    f"Could not validate checksum for row {row[self.id_field]}"
                )

        if (
                not self.ctx["config"]["nodes_only"]
                and (
                        self.ctx["config"]["paged_content_from_directories"] is False
                        or self.ctx["config"]["paged_content_from_directories_parents_exist"] is False
                )
        ):
            if not self.ctx["config"]["allow_missing_files"] and "file" in row:
                file_check = row["file"].strip()
                # Check for and log empty 'file' values.
                if len(file_check) == 0:
                    message = f"CSV row with ID {row[self.id_field]} contains an empty \"file\" value."
                    self.ctx["log"].warning(message)


            # Check for files that cannot be found.
            if not check_file_exists(self.ctx, row["file"]):
                message = f'File "{row["file"]}" identified in CSV "file" column for row with ID "{row[self.id_field]}" not found.'
                self.ctx["log"].error(message)
                self.testing_flags["rows_with_missing_files"] = True
                if self.ctx["config"]["allow_missing_files"] is False and self.ctx["config"]["perform_soft_checks"] is False:
                    raise WorkbenchValidationException(message)


            # Verify that all media bundles/types exist.
            if "file" in row and len(row["file"]) != 0:
                validate_media_type_and_extensions(self.ctx, self.id_field, row, "file")

            if "additional_files" in self.config_keys and len(self.ctx["config"]["additional_files"]) > 0:
                # Check existence of files named in columns identified as 'additional_files' columns.
                for additional_file_field in self.ctx["config"]["additional_files"]:
                    additional_file_field_value = row[additional_file_field].strip()
                    if len(additional_file_field_value) == 0:
                        message = (
                            f"CSV row with ID {row[self.id_field]} contains an empty value in its \"{additional_file_field}\" column."
                        )
                        self.ctx["log"].warning(message)

                    if not check_file_exists(self.ctx["config"], additional_file_field_value):
                        self.testing_flags["missing_additional_files"] = True
                        message = (
                            'Additional file "'
                            + additional_file_field_value
                            + '" in CSV column "'
                            + additional_file_field
                            + '" in row with ID '
                            + row[self.id_field]
                            + " not found or not accessible."
                        )
                        if not self.ctx["config"]["allow_missing_files"]:
                            self.ctx["log"].error(message)
                            if not self.ctx["config"]["perform_soft_checks"]:
                                raise WorkbenchValidationException(message)
                        else:
                            self.ctx["log"].error(message)

                    # Now validate the media type and bundles for each additional file fields.
                    if len(row[additional_file_field].strip()) > 0:
                        validate_media_type_and_extensions(self.ctx, self.id_field, row, additional_file_field)

    def finish_check(self):
        if self.testing_flags["rows_with_missing_files"]:
            if self.ctx["config"]["allow_missing_files"] is True:
                message = '"allow_missing_files" configuration setting is set to "true", and CSV "file" column values containing missing files were detected.'
                self.ctx["log"].warning(message)
        else:
            message = 'OK, files named in the CSV "file" column are all present.'
            self.ctx["log"].info(message)

        if self.testing_flags["missing_additional_files"]:
            if self.ctx["config"]["allow_missing_files"] is True:
                message = '"allow_missing_files" configuration setting is set to "true", and "additional_files" CSV columns containing missing files were detected.'
                self.ctx["log"].warning(message + " Details are logged.")
        else:
            message = 'OK, files named in "additional_files" CSV columns are all present.'
            self.ctx["log"].info(message)

    def prepare_run(self):
        if self.ctx["config"]["nodes_only"] is False and self.ctx["config"]["keep_filename_parent_directory"] is False:
            self.ctx["log"].info(
                "Parent directory names for files will not be used when creating Drupal media, but the directory names will appear in Workbench messages and log entries."
            )

    def process_row(self, row: Union[dict, str], row_number: int):
        if not value_is_numeric(row["node_id"]):
            row["node_id"] = get_nid_from_url_alias(self.ctx["config"], row["node_id"])
        if not ping_node(self.ctx["config"], row["node_id"]):
            self.ctx["log"].error(f"Node {row['node_id']} not found or not accessible, skipping adding media.")
            return

        node_uri = self.ctx["config"]["host"] + "/node/" + row["node_id"]

        if "media_use_tid" in row:
            media_use_tid_value = row["media_use_tid"]
        else:
            # Get media use TID from config within create_media().
            media_use_tid_value = None

        if len(row["file"].strip()) == 0:
            self.ctx["log"].warning(f"Media for node {row['node_id']} not created since CSV column 'file' is empty.")
        else:
            if not check_file_exists(self.ctx["config"], row["file"]):
                message = f'File "{row["file"]}" identified in CSV "file" column for node ID {row["node_id"]} not found.'
                self.ctx["log"].error(message)
                if not self.ctx["config"]["allow_missing_files"]:
                    raise WorkbenchException(message)
            else:
                media_response_status_code = create_media(
                    self.ctx["config"],
                    row["file"],
                    "file",
                    row["node_id"],
                    row,
                    media_use_tid_value,
                )
                if media_response_status_code in [201, 204]:
                    row_position_message = ""
                    if self.ctx["config"]["show_percentage_of_csv_input_processed"] is True:
                        row_position = int(row_number) / int(self.ctx["csv"].row_count) * 100
                        row_position_message = f" ({int(row_position)}%)"
                    message = f'Media for "{row["file"]}" created and added to node {node_uri}{row_position_message}'
                    self.ctx["log"].info(
                        message,
                        log_only=self.ctx["config"]["progress_bar"]
                    )
                else:
                    self.ctx["log"].error(
                        f"Media for {row['file']} not created. See log for more information.",
                        log_message=f"Media for {row['file']} not created (HTTP response code {media_response_status_code}).",
                        log_only=self.ctx["config"]["progress_bar"]
                    )

        # There are additional CSV columns naming files.
        if "additional_files" in self.config_keys and len(self.ctx["config"]["additional_files"]) > 0:
            for (
                additional_file_field,
                additional_file_media_use_tid,
            ) in self.ctx["additional_files_config"].items():
                if len(row[additional_file_field].strip()) == 0:
                    self.ctx["log"].warning(
                        f'Warning: Media for node "{row["node_id"]}" not created since CSV column "{additional_file_field}" is empty.',
                        log_only=self.ctx["config"]["progress_bar"]
                    )
                elif not check_file_exists(self.ctx["config"], row[additional_file_field]):
                    message = (
                        'Additional file "'
                        + row[additional_file_field]
                        + '" identified in CSV "'
                        + additional_file_field
                        + '" column for node ID '
                        + row["node_id"]
                        + " not found."
                    )
                    self.ctx["log"].error(message)
                    if not self.ctx["config"]["allow_missing_files"]:
                        raise WorkbenchException(message)
                    else:
                        continue
                else:
                    media_response_status_code = create_media(
                        self.ctx["config"],
                        row[additional_file_field],
                        additional_file_field,
                        row["node_id"],
                        row,
                        additional_file_media_use_tid,
                    )
                    if (
                        media_response_status_code
                        in [201, 204]
                    ):
                        self.ctx["log"].info(
                            f"Media for {row[additional_file_field]} created and added to {node_uri}.",
                            log_only=self.ctx["config"]["progress_bar"]
                        )

                    else:
                        self.ctx["log"].error(
                            f"Media for {row[additional_file_field]} not created (HTTP response code {media_response_status_code}).",
                            log_only=self.ctx["config"]["progress_bar"]
                        )

        if self.ctx["config"]["progress_bar"] is True:
            self.ctx["progress_bar"].updateAmount(row_number)
