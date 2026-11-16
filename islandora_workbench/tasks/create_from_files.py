import copy
import json
import os
from typing import Union

from progress_bar import InitBar

from islandora_workbench.data_io.rollback import RollbackMgr
from islandora_workbench.drupal.fields import get_required_bundle_fields, get_field_definitions
from islandora_workbench.exceptions import WorkbenchValidationException
from islandora_workbench.tasks import register_task
from islandora_workbench.tasks.base import Task
from islandora_workbench.utils import value_is_numeric
from workbench_utils import set_model_from_extension, set_media_type
from islandora_workbench.external_scripts import execute_entity_post_task_script
from islandora_workbench.drupal.client import get_entity_fields
from islandora_workbench.drupal.node import create_node
from islandora_workbench.drupal.media import create_media
from islandora_workbench.data_io.output_csv import OutputCSVWriter



@register_task("create_from_files")
class CreateFromFilesTask(Task):

    name = "create_from_files"
    required_config_keys = ("task", "host", "username", "password")

    def __init__(self, ctx: dict):
        super().__init__(ctx)
        self.required_fields = tuple(get_required_bundle_fields(
            self.ctx["config"], "node", self.ctx["config"]["content_type"]
            ))
        self.field_configs["entity_field_definitions"] = get_field_definitions(self.ctx["config"], "node")
        if "additional_files" in self.ctx["config"] and len(self.ctx["config"]["additional_files"]) > 0:
            self.field_configs["additional_files"] = self.ctx["config"]["additional_files"]
        else:
            self.field_configs["additional_files"] = {}
        if "output_csv" in self.config_keys:
            fields_to_exclude = copy.copy(self.RESERVED_FIELDS)
            fields_to_exclude.extend(self.field_configs["additional_files"].keys())
            self.ctx["output_csv_writer"] = OutputCSVWriter(self.ctx, "ID", fields_to_exclude)

    def general_checks(self):
        # Remove additional config keys that are not relevant for the create_from_files task.
        unwanted_in_create_from_files = ["delimiter", "subdelimiter", "allow_missing_files",
            "paged_content_from_directories", "paged_content_from_directories_parents_exist", "delete_media_with_nodes",
            "allow_adding_terms", ]
        self.config_keys = [key for key in self.config_keys if key not in unwanted_in_create_from_files]

        if os.path.exists(self.ctx["config"]["input_dir"]) and os.path.isdir(self.ctx["config"]["input_dir"]):
            message = 'OK, input directory "' + self.ctx["config"]["input_dir"] + '" found.'
            self.ctx["log"].info(message)
        else:
            message = f'Input directory "{self.ctx["config"]["input_dir"]}" not found.'
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

        # Validate length of 'title'.
        files = os.listdir(self.ctx["config"]["input_dir"])
        for file_name in files:
            filename_without_extension = os.path.splitext(file_name)[0]
            if len(filename_without_extension) > int(self.ctx["config"]["max_node_title_length"]):
                message = (
                        'The filename "' + filename_without_extension + "\" exceeds Drupal's maximum length of " +
                        self.ctx["config"]["max_node_title_length"] + " characters and cannot be used for a node title.")
                self.ctx["log"].error(message)
                raise WorkbenchValidationException(message)

        # Check that either 'model' or 'models' are present in the config file.
        if "model" not in self.config_keys and "models" not in self.config_keys:
            message = 'You must include either the "model" or "models" option in your configuration.'
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

        self.field_configs["entity_fields"] = list(
            get_entity_fields(self.ctx['config'], "node", self.ctx['config']["content_type"]).keys()
        )

    def rows(self):
        # No CSV for create_from_files, so return an iterator over the files in the input directory.
        file_dir_path = self.ctx["config"]["input_dir"]
        files = os.listdir(file_dir_path)
        for file in files:
            yield file

    def prepare_run(self):
        # Prepare for the create_from_files task (e.g., validate input, set up environment)
        self.ctx["rollback"] = RollbackMgr(self.ctx)
        if self.ctx["config"]["progress_bar"] is True:
            row_count = len(os.listdir(self.ctx["config"]["input_dir"])) if os.path.exists(
                self.ctx["config"]["input_dir"]
                ) else 0
            self.ctx["progress_bar"] = InitBar(size=row_count, title=f"Running {self.name} task")

    def process_row(self, row: Union[dict, str], row_number: int):
        # Implement the logic to process each row for the create_from_files task
        file_name = str(row)  # For readability, we can refer to the row as file_name since it's a file name in this context.
        if (
            self.ctx["config"]["recovery_mode_starting_from_node_id"] is not False
            and value_is_numeric(self.ctx["config"]["recovery_mode_starting_from_node_id"]) is True
        ):
            nid_in_map = self.ctx["node_map"].get_node_id_recovery_mode(file_name)
            if nid_in_map is not False or not None:
                message = f"File {file_name} has already been ingested at ({self.ctx['config']['host']}/node/{nid_in_map}), skipping it."
                self.ctx["log"].info(message)
                return
            if file_name.startswith("rollback.") and file_name.endswith("csv"):
                return
            if self.ctx["config"]["csv_id_to_node_id_map_path"] is not False and str(file_name).endswith(
                    self.ctx["config"]["csv_id_to_node_id_map_path"]
            ):
                return

        filename_without_extension = os.path.splitext(file_name)[0]
        if len(filename_without_extension) > self.ctx["config"]["max_node_title_length"]:
            message = (
                    'Truncating the filename "' + filename_without_extension + '" since it exceeds maximum node title length of ' + str(
                self.ctx["config"]["max_node_title_length"]
                ) + " characters.")
            self.ctx["log"].error(message)
            filename_without_extension = filename_without_extension[:255]

        node_json = {"type": [{"target_id": self.ctx["config"]["content_type"], "target_type": "node_type"}],
            "title": [{"value": filename_without_extension}], }

        entity_fields = get_entity_fields(self.ctx["config"], "node", self.ctx["config"]["content_type"]).keys()
        if "field_model" in entity_fields:
            islandora_model = set_model_from_extension(file_name, self.ctx["config"])
            node_json["field_model"] = [{"target_id": islandora_model, "target_type": "taxonomy_term"}]

        response_data, response_meta = create_node(self.ctx, node_json)

        if response_meta["status_code"] != 201:
            self.ctx["log"].error(
                f'Node for "{os.path.join(self.ctx["config"]["input_dir"], file_name)}" not created, HTTP response code was {response_meta["status_code"]}.'
            )
            self.ctx["log"].error(
                f'JSON request body used in previous POST to "{self.ctx["config"]["host"]}/node?_format=json" was {node_json}.', log_only=True
            )
        else:
            returned_node = response_data
            node_id = returned_node["nid"][0]["value"]
            node_uri = self.ctx["config"]["host"] + "/node/" + str(node_id)

            self.ctx["log"].info(f'Node for "{filename_without_extension}" created at {node_uri}.', log_only=self.ctx["config"]["progress_bar"])
            if "output_csv" in self.config_keys:
                self.ctx["output_csv_writer"].write_to_output_csv("", response_data)

            node_nid = node_uri.rsplit("/", 1)[-1]

            self.ctx["rollback"].write_rollback_node_id(node_nid, "", filename_without_extension, file_name, "")
            self.ctx["node_map"].add_mapping("", "", node_nid, file_name)

            # Execute node-specific post-create scripts, if any are configured.
            if "node_post_create" in self.config_keys and len(self.ctx["config"]["node_post_create"]) > 0:
                for command in self.ctx["config"]["node_post_create"]:
                    (post_task_output, post_task_return_code,) = execute_entity_post_task_script(
                        command, self.ctx["config"]["config_file"], response_meta["status_code"], json.dumps(response_data)
                    )
                    if post_task_return_code == 0:
                        self.ctx["log"].info(
                            f"Post node create script {command} executed successfully."
                        )
                    else:
                        self.ctx["log"].error(f"Post node create script {command} failed.")

            file_path = os.path.join(self.ctx["config"]["input_dir"], file_name)
            fake_csv_record = {
                "title": filename_without_extension,
                "file": file_path
            }
            media_type = set_media_type(self.ctx["config"], file_path, "file", fake_csv_record)

            if media_type == "image":
                fake_csv_record["image_alt_text"] = filename_without_extension

            media_response_status_code = create_media(
                self.ctx["config"], file_name, "file", node_nid, fake_csv_record
            )
            if media_response_status_code in [201, 204]:
                self.ctx["log"].info(
                    f"Media for {file_path} created.",
                    log_only=self.ctx["config"]["progress_bar"],
                )

            if self.ctx["config"]["progress_bar"] is True:
                self.ctx["progress_bar"].updateAmount(1)

    def check_row(self, row: Union[dict, str], row_number: int):
        # No CSV so no row checking for create_from_files.
        pass
