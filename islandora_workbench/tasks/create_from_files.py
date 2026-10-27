import os
import sys

from islandora_workbench.tasks import register_task
from islandora_workbench.tasks.base import Task
from workbench_utils import check_for_required_config_keys, is_running_in_docker, value_is_numeric, \
    check_rollback_file_path_directories

@register_task("create_from_files")
class CreateFromFilesTask(Task):
    def __init__(self, ctx: dict):
        super().__init__(ctx)
        self.ctx = ctx

    def check(self):
        super().check()

        unwanted_in_create_from_files = ["delimiter", "subdelimiter", "allow_missing_files",
            "paged_content_from_directories", "paged_content_from_directories_parents_exist", "delete_media_with_nodes",
            "allow_adding_terms", ]
        self.config_keys = [key for key in self.ctx.config.keys() if key not in unwanted_in_create_from_files]

        create_required_options = ["task", "host", "username", "password"]
        try:
            check_for_required_config_keys(self.ctx.config, create_required_options, throw_exception=True)
        except KeyError as e:
            self.ctx.log.error(f"Missing required configuration key: {e}")
            sys.exit(1)  # todo: replace with exception

        if os.path.exists(self.ctx.config["input_dir"]) and os.path.isdir(self.ctx.config["input_dir"]):
            message = 'OK, input directory "' + self.ctx.config["input_dir"] + '" found.'
            self.ctx.log.info(message)
        else:
            message = 'Input directory "' + self.ctx.config["input_dir"] + '"" not found.'
            self.ctx.log.error(message)
            sys.exit(1)  # todo: replace with exception

        # Validate length of 'title'.
        files = os.listdir(self.ctx.config["input_dir"])
        for file_name in files:
            filename_without_extension = os.path.splitext(file_name)[0]
            if len(filename_without_extension) > int(self.ctx.config["max_node_title_length"]):
                message = (
                        'The filename "' + filename_without_extension + "\" exceeds Drupal's maximum length of " +
                        self.ctx.config["max_node_title_length"] + " characters and cannot be used for a node title.")
                self.ctx.log.error(message)
                sys.exit(1)  # todo: replace with exception

        # Check that either 'model' or 'models' are present in the config file.
        if "model" not in self.ctx.config and "models" not in self.ctx.config:
            message = 'You must include either the "model" or "models" option in your configuration.'
            self.ctx.log.error(message)
            sys.exit(1)  # todo: replace with exception

        # If nothing has failed by now, exit with a positive message.
        always_review_log_message = "" if self.ctx.config["perform_soft_checks"] is True else " However, you should review your Workbench log after running --check."
        config_and_data_appear_to_be_valid_message = (
            f"Configuration and input data appear to be valid.{always_review_log_message}")
        self.ctx.log.info(config_and_data_appear_to_be_valid_message)

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

        check_rollback_file_path_directories(self.ctx.config)



    def prepare(self):
        # Prepare for the create_from_files task (e.g., validate input, set up environment)
        pass

    def rows(self):
        # Yield rows of data to be processed for the create_from_files task
        pass

    def process_row(self, row):
        # Implement the logic to process each row for the create_from_files task
        pass

    def finish(self):
        # Finalize the create_from_files task (e.g., summarize results, clean up)
        pass