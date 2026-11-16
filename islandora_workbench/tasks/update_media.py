from typing import Union

from islandora_workbench.checks.validation import validate_media_use_tids_in_csv_action, validate_media_track_value
from islandora_workbench.drupal.client import ping_media, create_file, get_term_id_from_uri, ping_term, \
    is_revisions_enabled
from islandora_workbench.drupal.fields import get_field_definitions, WorkbenchFieldFactory
from islandora_workbench.drupal.file import delete_file
from islandora_workbench.drupal.media import get_media, find_file_id_in_media, delete_media_file, patch_media, \
    get_media_field_values
from islandora_workbench.exceptions import WorkbenchValidationException
from islandora_workbench.tasks import Task, register_task
from islandora_workbench.tasks.base import validate_media_type_and_extensions
from islandora_workbench.utils import check_file_exists, value_is_numeric


@register_task("update_media")
class UpdateMediaTask(Task):

    name = "update_media"
    required_fields = ("media_id")
    required_config_keys = ("task", "host", "username", "password", "input_csv", "media_type")

    def __init__(self, ctx: dict):
        super().__init__(ctx)
        self.field_configs["entity_field_definitions"] =  get_field_definitions(self.ctx["config"], "media", self.ctx["config"]["media_type"])
        self.field_configs["additional_files"] = self.ctx["config"].get("additional_files", {})
        self.id_field = self.ctx["config"].get("id_field", "media_id")
        self.check_log_message = f"Starting {self.ctx['config']['task']} task ({self.ctx['config']['update_mode']}) using config file: {self.ctx['config']['config_file_path']}"

    def general_checks(self):
        if self.ctx["config"]["nodes_only"] is False and self.ctx["config"]["keep_filename_parent_directory"] is False:
            self.ctx["log"].info(
                "Parent directory names for files will not be used when creating Drupal media, but the directory names will appear in Workbench messages and log entries."
            )

        base_media_fields = ["status", "uid", "langcode"]
        possible_fields = base_media_fields + list(self.field_configs["entity_field_definitions"].keys()) + list(self.required_fields)
        extra_fields = [f for f in self.ctx["csv"].field_names if f not in possible_fields]
        if len(extra_fields) > 0:
            message = f'CSV column header(s) "{", ".join(extra_fields)}" does not match any Drupal field names in the "{self.ctx["config"]["media_type"]}" media type'
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

        self.ctx["log"].info("OK, CSV column headers match Drupal field names.")


    def check_row(self, row: Union[dict, str], row_number: int):

        row_level_errors = []
        row_id = row[self.id_field]

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

                validate_media_type_and_extensions(self.ctx, self.id_field, row, "file")


    def prepare_run(self):
        if self.ctx["config"]["nodes_only"] is False and self.ctx["config"]["keep_filename_parent_directory"] is False:
            self.ctx["log"].info(
                "Parent directory names for files will not be used when creating Drupal media, but the directory names will appear in Workbench messages and log entries."
            )

    def process_row(self, row: Union[dict, str], row_number: int):
        media_id = row[self.id_field]
        if media_id is None:
            message = f"Row {row_number} is missing a media_id field value. Skipping this row."
            self.ctx["log"].error(message)
            return

        # Now, the user may want to update one or more of the following
        # - Media File
        # - Track File
        # - Media Use TID
        # - Published status
        # - Plain text fields pertaining to the media.

        media_body = get_media(self.ctx, media_id)
        # From this we can get the media type, which we'll need as well.
        try:
            media_type = media_body["bundle"][0]["target_id"]
        except KeyError:
            message = f"Row {row_number} has a media_id of {media_id}, but the media type could not be determined. Skipping this row."
            self.ctx["log"].error(message)
            return

        # Populate the media JSON that we PATCH with, which gets added to below.
        patch_request_json = {"bundle": [{"target_id": media_type}]}

        try:
            # Return the first node ID in the list of nodes the media entity is attached to
            node_id = media_body["field_media_of"][0]["target_id"]
        except KeyError:
            self.ctx["log"].error(f"Unable to get parent node ID for media ID {media_id}. Skipping this row.")
            return

        # At this point we have the node ID of the parent node of the media.
        # We use this with the create_file function to create the media file on the server.
        file_id = create_file(self.ctx["config"], row["file"], "file", row, node_id)
        if file_id is False or file_id is None:  # If the file ID is invalid, skip this row.
            self.ctx["log"].error(
                f"Failed to create file for media ID {media_id}. Skipping this row."
            )
            return

        # Get the name of the field that corresponds to the media type (e.g. 'field_media_image' for media type 'image')
        try:
            media_field = self.ctx["config"]["media_type_file_fields"][media_type]
            patch_request_json.update({media_field: [{"target_id": file_id, "target_type": "file", }]})
        except KeyError:
            self.ctx["log"].error(
                f"Media type {media_type} does not have a corresponding file field in the \"media_type_file_fields\" configuration. Skipping this row."
            )
            return

        # Update track file.
        if media_field in row and row[media_field] != "":
            # There may be multiple track files specified, separated by a delimeter. Add the information for each of these to a list.
            track_files = row[media_field].split(self.ctx["config"]["subdelimiter"])
            # Now make a list to hold the track JSON.
            track_files_info = []
            # Loop over every track file and add the information to the lists.
            for track_file in track_files:
                if not validate_media_track_value(track_file):
                    self.ctx["log"].error(
                        f"Invalid track file value for media ID {media_id}. Skipping this row."
                    )
                    return
                track_label, track_type, track_language, track_filepath = track_file.split(":")

                # From the track file path, we can upload the file to the server and get the file ID.
                file_id = create_file(
                    self.ctx["config"], track_filepath, self.ctx["config"]["media_track_file_fields"][media_type], row, node_id
                )
                if not file_id:
                    self.ctx["log"].error(
                        f"Failed to create file for media ID {media_id}. Skipping this row."
                    )
                    return
                track_files_info.append({
                    "target_id": file_id,
                    "label": track_label,
                    "kind": track_type,
                    "srclang": track_language,
                    "target_type": "file"
                })

            # Now we'll get the JSON for the PATCH request to the track_file_field_name to update the track files.
            patch_request_json.update(
                {
                    self.ctx["config"]["media_track_file_fields"][media_type]: track_files_info
                }
            )

        # Update media use tid.
        if "media_use_tid" in row:
            if row["media_use_tid"] != "":
                # User expects us to update media use tid to the one provided in the CSV.
                media_use_tids = row["media_use_tid"].split(self.ctx["config"]["subdelimiter"])
            else:
                # User expects us to update media use tid to the default media use tid.
                media_use_tids = str(self.ctx["config"]["media_use_tid"]).split(
                    self.ctx["config"]["subdelimiter"]
                )

            # Iterate through the list of media_use_tid values and process each tid.
            term_ids = []
            for media_tid in media_use_tids:
                if not value_is_numeric(media_tid):
                    # Note that this call checks if the term exists by pinging it and returns False if it doesn't
                    tid = get_term_id_from_uri(self.ctx["config"], media_tid)
                    if not tid:  # If media use term URL alias does not exist, skip updating media
                        self.ctx["log"].error(
                            f"Media use term URL alias {media_tid} not found or not accessible, skipping updating media.",
                        )
                        return

                else:  # User has specified a numeric media use tid
                    # Check if the media use term exists by pinging it
                    if not ping_term(self.ctx["config"], media_tid):
                        self.ctx["log"].error(
                            f"Media use term {media_tid} not found or not accessible, skipping updating media.",
                        )
                        return
                    tid = media_tid
                # As get_term_id_from_uri returns the tid as an int, we need to convert it to a string
                term_ids.append(str(tid))

            # By this point we have a valid media ID and media use tid, so we can update the media use tid.
            patch_request_json.update({
                "field_media_use": [
                    {"target_id": tid, "target_type": "taxonomy_term"}
                    for tid in term_ids
                ]
            })
            # This call returns a dict with the JSON for the PATCH request to update the media use tid

        # Update media status.
        if "status" in row and row["status"] != "":
            if row["status"].lower() not in ["1", "true", "0", "false"]:
                self.ctx["log"].error(
                    f"Invalid value for published status in row {row_number}. Must be 1, 0, true, or false. Skipping this row."
                )
                return

            patch_request_json.update(
                {"status": [{"value": row["status"].lower() in ["1", "true"] }]}
            )

        # Add custom (non-required) fields.
        base_media_fields = ["status", "uid", "langcode"]
        possible_fields = base_media_fields + list(self.field_configs["entity_field_definitions"].keys()) + list(
            self.required_fields
            )
        custom_fields = [f for f in self.ctx["csv"].field_names if f not in possible_fields]
        if len(custom_fields) > 0:
            ping_media_result = ping_media(self.ctx["config"], media_id, "GET", True)
            if not ping_media_result:
                self.ctx["log"].error(
                    f"Media {media_id} not found or not accessible, skipping updating media.",
                )
                return
            reserved_fields = ["published", "url_alias", "file", "revision_log", "node_id", ]
            media_field_values = get_media_field_values(self.ctx["config"], row["media_id"])
            for custom_field in custom_fields:
                media_has_all_fields = True
                # If media doesn't have the field, log that fact and skip updating the field.
                if custom_field not in ping_media_result.keys() and custom_field not in reserved_fields:
                    message = f'Media {row["media_id"]} does not have a "{custom_field}" field, skipping update.'
                    self.ctx["log"].warning(message)
                    continue

                # Skip updating field if CSV field is empty (other than for 'delete' update mode).
                # For 'delete' update mode it doesn't matter if there's anything in the CSV field,
                # but users expect to be able to supply empty values for this operation.
                if len(row[custom_field].strip()) == 0 and self.ctx["config"]["update_mode"] != "delete":
                    continue

                if custom_field not in reserved_fields:
                    # Assemble Drupal field structures from CSV data. If new field types are added to
                    # fields.py, they need to be registered in the following if/elif/else block.

                    field = WorkbenchFieldFactory.get_field_handler(
                        self.field_configs["entity_field_definitions"][custom_field]["field_type"]
                    )
                    patch_request_json = field.update(
                        self.ctx["config"],
                        self.field_configs,
                        patch_request_json,
                        row,
                        custom_field,
                        media_field_values[custom_field],
                    )

        # Before the patch requests, we delete the existing media/track files if the user wants to replace them.
        # Delete the old media file.
        # Delete the old file which was attached to this media from the server.
        file_id = find_file_id_in_media(media_body)
        if file_id is not None:
            if not delete_media_file(self.ctx["config"], media_id, file_id):
                self.ctx["log"].error(
                    f"Failed to find existing file for media ID {media_id}. Skipping this row."
                )
                return

        # Delete the old track files.
        # Delete the old track files which were attached to this media from the server.
        if not self.delete_media_track_files(media_id, media_type, media_body):
            self.ctx["log"].error(
                "Media at " + self.ctx["config"][
                    "host"] + "/media/" + media_id + " could not be updated. Please check the log for more details."
            )
            return

        # Update revision log message.
        if is_revisions_enabled(self.ctx["config"], "media", media_type):
            revision_log_message = (row["revision_log"] if "revision_log" in row and row[
                "revision_log"] != "" else "Updated by Islandora Workbench.")
            revision_log_message_key = "revision_log_message"
            patch_request_json.update(
                {revision_log_message_key: [{"value": revision_log_message}]}
            )

        # Make the PATCH request.
        patch_response = patch_media(self.ctx, media_id, patch_request_json)
        if patch_response.get("status_code", 0) == 204:
            self.ctx["log"].info(
                f"Media at {self.ctx['config']['host']}/media/{media_id} updated successfully.",
                log_only=True
            )
        else:
            self.ctx["log"].error(
                f"Media at {self.ctx['config']['host']}/media/{media_id} could not be updated. Please check the log for more details."
            )

        if self.ctx["progress_bar"] is not None:
            self.ctx["progress_bar"].updateAmount(row_number)


    def delete_media_track_files(
            self, media_id: str, media_type: str, get_media_response_body: dict
    ) -> bool:
        """Delete the track files attached to the media entity.

        Parameters:
            - config: The global configuration object.
            - media_id: A valid media entity ID.
            - media_type: The media entity's type.
            - get_media_response_body: The response body from a GET request to the media entity's endpoint.

        Returns:
            True if the track file was successfully deleted or if there were no track files in the first place, False otherwise.
        """
        # Inspect the JSON response to get the file ID
        if self.ctx["config"]["media_track_file_fields"][media_type] in get_media_response_body:
            for track_file in get_media_response_body[self.ctx["config"]["media_track_file_fields"][media_type]]:
                try:
                    file_to_delete = str(track_file["target_id"])
                except Exception as e:  # There is a track file attached to this media, but we can't get its ID
                    self.ctx["log"].warning(
                        f'Unable to get track file ID for a track file attached to media ID "{media_id}" ({e}).',
                        log_only=True
                    )
                    return False

                delete_response = delete_file(self.ctx["config"], file_to_delete)
                if delete_response:
                    self.ctx["log"].info(
                        f'Successfully deleted track file with ID "{file_to_delete}" attached to media ID "{media_id}".',
                        log_only=True
                    )
                else:
                    self.ctx["log"].warning(
                        f'Unable to delete track file with ID "{file_to_delete}" attached to media ID "{media_id}".',
                        log_only=True
                    )
                return delete_response
        else:
            self.ctx["log"].warning(
                f"Unable to find track files for media ID {media_id}. Proceeding and assuming there were no track files to begin with.",
                log_only=True
            )
        return True