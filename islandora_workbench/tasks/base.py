import os
import sys
from abc import abstractmethod, ABC
from csv import DictReader

from workbench_utils import get_csv_data, get_entity_fields, validate_input_dir, check_csv_file_exists, \
    get_preprocessed_input_csv_file_path


class Task(ABC):
    """Base class for all tasks. Each task is a subclass of this class, and implements the methods below."""

    # task name
    name: str
    # context dictionary
    ctx: dict
    # configuration keys
    config_keys: list[str]

    node_base_fields = ["title", "status", "promote", "sticky", "uid", "created", "published", ]

    reserved_fields = ["file", "directory", "media_use_tid", "checksum", "node_id", "url_alias", "image_alt_text",
        "parent_id", "langcode", "revision_log", ]

    entity_fields: list[str]

    def __init__(self, ctx: dict):
        self.ctx = ctx
        self.entity_fields = get_entity_fields(self.ctx.config, "node", self.ctx.config["content_type"])
        if self.ctx.config["id_field"] not in self.entity_fields:
            self.reserved_fields.append(self.ctx.config["id_field"])

    def check(self):
        """Perform task-specific checks, called from checks.run_checks."""
        self.config_keys = list(self.ctx.config.keys())
        self.config_keys.remove("check")

    def prepare(self):
        """Prepare the task for execution. (i.e. rollback files, ID map, csv load, etc.)"""
        pass

    @abstractmethod
    def rows(self):
        """yields rows after start/stop filtering"""
        pass

    @abstractmethod
    def process_row(self, row):
        """The per-row work; each task overrides this."""
        pass

    def finish(self):
        """Summary, post-run output."""
        pass

    def run(self):
        """shared lifecycle; also owns progress bar and log lines"""
        self.prepare()
        for row in self.rows():
            self.process_row(row)
        self.finish()

    def row_column_checks(self, csv_data: DictReader, csv_column_headers: list) -> None:
        """Check whether each row of the CSV data contains the same number of columns as there are headers.
        :param csv_data: DictReader object containing the CSV data.
        :param csv_column_headers: List of column headers from the CSV file.
        """
        row_count = 0
        for row_count, row in enumerate(csv_data, start=1):
            extra_headers = False
            field_count = 0
            for field in row:
                # 'stringtopopulateextrafields' is added by get_csv_data() if there are extra headers.
                if row[field] == "stringtopopulateextrafields":
                    extra_headers = True
                else:
                    field_count += 1
            if extra_headers is True:
                message = ("Row " + str(row_count) + " (ID " + row[
                    self.ctx.config["id_field"]] + ") of the CSV file has fewer columns " + "than there are headers (" + str(
                    len(csv_column_headers)
                ) + ").")
                self.ctx.log.error(message)
                sys.exit("Error: " + message)
            # Note: this message is also generated in get_csv_data() since CSV Writer thows an exception if the row has form fields than headers.
            if len(csv_column_headers) < field_count:
                message = ("Row " + str(row_count) + " (ID " + row[
                    self.ctx.config["id_field"]] + ") of the CSV file has more columns (" + str(
                    field_count
                ) + ") than there are headers (" + str(len(csv_column_headers)) + ").")
                self.ctx.log.error(message)
                sys.exit("Error: " + message)
        if row_count == 0:
            message = f"Input CSV file {self.ctx.config['input_csv']} has 0 rows."
            self.ctx.log.error(message)
            sys.exit("Error: " + message)
        else:
            message = ("OK, all " + str(
                row_count
            ) + " rows in the CSV file have the same number of columns as there are headers (" + str(
                len(csv_column_headers)
            ) + ").")
            self.ctx.log.info(message)

    def row_filter_checks(self):
        """Perform checks on row filter settings."""
        global commented_out_input_csv_rows_present
        row_filter_settings = []
        if self.ctx.config["csv_start_row"] != 0:
            row_filter_settings.append("csv_start_row")
        if self.ctx.config["csv_stop_row"] is not None:
            row_filter_settings.append("csv_stop_row")
        if "csv_rows_to_process" in self.ctx.config:
            row_filter_settings.append("csv_rows_to_process")
        if "csv_row_filters" in self.ctx.config:
            row_filter_settings.append("csv_row_filters")
        if commented_out_input_csv_rows_present is True:  # todo: don't use global variable set in get_csv_data()
            row_filter_settings.append(True)
        if "csv_start_row_skip" in self.ctx.config:
            row_filter_settings.append(True)
        if "csv_stop_row_skip" in self.ctx.config:
            row_filter_settings.append(True)
        if len(row_filter_settings) > 1:
            preprocessed_input_csv_file_path = get_preprocessed_input_csv_file_path(self.ctx.config)
            message = f'Your configuration contains more than one input CSV row filter setting, and/or your input CSV has some commented-out rows. Please check "{preprocessed_input_csv_file_path}" to confirm the rows you want are present.'
            self.ctx.log.warning(message)

    def postprocess_script_checks(self):
        post_action_scripts_configs = ["node_post_create", "node_post_update", "media_post_create", ]
        post_action_scripts_present = False
        for post_action_script_config in post_action_scripts_configs:
            if post_action_script_config in self.ctx.config and len(self.ctx.config[post_action_script_config]) > 0:
                post_action_scripts_present = True
                for post_action_script in self.ctx.config[post_action_script_config]:
                    if " " in post_action_script:
                        post_action_script = post_action_script.split(" ")[-1]
                    if not os.path.exists(post_action_script) or not os.path.isfile(post_action_script):
                        self.ctx.log.error(f"Post-action script {post_action_script} not found.")
                        sys.exit(1)  # todo: replace with an exception
                    if not os.access(post_action_script, os.X_OK):
                        self.ctx.log.error(f"Post-action script {post_action_script} is not executable.")
                        sys.exit(1)  # todo: replace with an exception
            if post_action_scripts_present is True:
                self.ctx.log.info("OK, registered post-action scripts found and executable.")
