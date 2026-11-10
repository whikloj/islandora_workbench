import json
import os
from abc import abstractmethod, ABC
from typing import Union

import requests_cache

from islandora_workbench.checks.common import common_checks_before_task, common_checks_after_task
from islandora_workbench.checks.csv_checks import check_row_field_count, ParentPrecedesChildCheck
from islandora_workbench.data_io.csv.csv_reader import CsvReader
from islandora_workbench.drupal.client import ping_islandora
from islandora_workbench.exceptions import WorkbenchValidationException, WorkbenchException
from islandora_workbench.utils.remote_utils import download_remote_archive_file
from islandora_workbench.utils.utils import get_file_hash_from_local, check_for_required_config_keys, prompt_user, \
    unzip_archive, check_drupal_core_version
from islandora_workbench.internal import create_temp_dir, check_integration_module_version
from workbench_checks import _generate_geolocation_field_list, _generate_link_field_list, \
    _generate_authority_link_field_list, _generate_edtf_field_list, _generate_taxonomy_term_reference_field_list, \
    _generate_typed_relation_field_list, _generate_cardinality_field_list, _generate_max_length_field_list, \
    _generate_text_with_allowed_field_list, _generate_numeric_field_list, _generate_media_field_list



class Task(ABC):
    """Base class for all tasks. Each task is a subclass of this class, and implements the methods below.

    Task allows for checking and running.
    Check:
    - prepare() - prepare the task for execution
    - common_checks_before_task() - run common checks before the task
    - general_checks() - run task specific checks
    - check_row() - run task specific checks for each row in the CSV
    - common_checks_after_task() - run common checks after the task
    - finish() - finish the task

    Run:
    - prepare() - prepare the task for execution
    - prepare_run() - run task specific preparation for execution
    - process_row() - run task specific processing of each row in the CSV
    - finish() - finish the task
    """

    # task name
    name: str
    # context dictionary
    ctx: dict
    # The field in the CSV that contains the id
    id_field: str
    # required fields
    required_fields: tuple[str, ...] = ()
    # required configuration keys
    required_config_keys: tuple[str, ...] = ()
    # configuration keys
    config_keys: list[str]
    # Does this task use a CSV file? (default: True)
    is_use_csv: bool = True
    # Are we doing validation (--check)? (default: False)
    is_check_mode: bool = False
    # Field configurations
    field_configs: dict
    # Various field types to make validation of these fields that exist easier, see generate_field_lists()
    field_types: dict
    # testing flags to allow passing checks run at general check level to the row_check level.
    testing_flags: dict
    # list of base fields for nodes.
    NODE_BASE_FIELDS = ["title", "status", "promote", "sticky", "uid", "created", "published"]
    # list of reserved fields that should not be used as entity fields.
    RESERVED_FIELDS = ["file", "directory", "media_use_tid", "checksum", "node_id", "url_alias", "image_alt_text",
                        "parent_id", "langcode", "revision_log", ]



    def __init__(self, ctx: dict):
        self.ctx = ctx
        # configuration keys
        self.config_keys = []
        # ID field
        self.id_field = self.ctx["config"]["id_field"] if "id_field" in self.ctx["config"] else "ID"
        # Field configurations
        self.field_configs: dict = {  # list of fields that are valid for the entity type and bundle being processed.
            "entity_fields": [],  # entity field definitions for the entity type and bundle being processed.
            "entity_field_definitions": {},  # dictionary of additional files configuration for easy access.
            "additional_files": {},  # dictionary of required fields for the entity type and bundle being processed.
            "required_bundle_fields": {},  # media bundle field definitions
            "media_bundle_field_definitions": {},
            "reserved_fields": self.RESERVED_FIELDS,  # reserved fields that should not be used as entity fields.
        }
        # Various field types to make validation of these fields that exist easier, see generate_field_lists()
        self.field_types: dict = {}
        # testing flags to allow passing checks run at general check level to the row_check level.
        self.testing_flags: dict = {}
        self.general_setup()

    def general_setup(self):
        """General setup for all tasks."""
        self.config_keys = [k for k in self.ctx["config"].keys() if k not in ["check", "check_lock_file_path", "secondary_tasks", "contactsheet", "contact_sheet_output_dir"]]

    def generate_field_lists(self, csv_columns: list):
        """Generate the lists of the various types of fields that exist in the CSV."""
        self.field_types["geolocation"] = _generate_geolocation_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["link"] = _generate_link_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["authority_link"] = _generate_authority_link_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["edtf"] = _generate_edtf_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["taxonomy_term_reference"] = _generate_taxonomy_term_reference_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["typed_relation"] = _generate_typed_relation_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["cardinality"] = _generate_cardinality_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["allowed_values"] = _generate_text_with_allowed_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["max_length"] = _generate_max_length_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["numeric_fields"] = _generate_numeric_field_list(self.field_configs["entity_field_definitions"], csv_columns)
        self.field_types["media_fields"] = _generate_media_field_list(self.ctx["config"], csv_columns)



    def validate_required_fields(self):
        """Validate the fields in the CSV against the task requirements.
        :raises WorkbenchCsvReaderException: If required fields are missing.
        """
        csv_field_names = self.ctx["csv"].field_names
        missing_fields = [field for field in self.required_fields if field not in csv_field_names]
        if missing_fields:
            message = f"Task {self.__class__.__name__}: Required fields {', '.join(missing_fields)} are missing from the CSV file. Please check your input CSV file and try again."
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)

    def check_lock_file_setup(self):
        """Check the setup of the lock file."""
        if "check_lock_file_path" in self.ctx["config"]:
            check_lock_file_path = self.ctx["config"]["check_lock_file_path"]
            if os.path.exists(check_lock_file_path) and os.path.isfile(check_lock_file_path):
                config_file_md5 = get_file_hash_from_local(self.ctx["config"]["config_file_path"], "md5")
                with open(check_lock_file_path, "r") as check_lock_file:
                    check_lock_message_expected = f"Check against {self.ctx['config']['config_file_path']} (md5 hash {config_file_md5}) OK"
                    check_lock_message_from_file = check_lock_file.readline()
                    if check_lock_message_expected.strip() != check_lock_message_from_file.strip():
                        message = f'The "check_lock_file_path" setting is present in config, but the contents of the lock file ("{check_lock_message_from_file.strip()}") do not match the expected --check lock message ("{check_lock_message_expected.strip()}").'
                        self.ctx["log"].error(message)
                        raise WorkbenchException(message)
                    else:
                        message = f'Check lock file "{check_lock_file_path}" confirms --check was run. Removing lock file.'
                        self.ctx["log"].info(message)
                        os.remove(check_lock_file_path)
            else:
                message = f'The "check_lock_file_path" setting is present in config but the specified lock file, "{check_lock_file_path}", does not exist.'
                self.ctx["log"].error(message)
                raise WorkbenchException(message)

    def check(self):
        """Perform checks."""
        self.prepare()

        if self.is_use_csv:
            try:
                csv = CsvReader.open(self.ctx['config'])
            except FileNotFoundError as e:
                self.ctx["log"].error(str(e))
                raise WorkbenchException(str(e))
            self.ctx["csv"] = csv
            # Checking that all rows have the same number of columns as there are headers occurs when generating the CSV reader, so we can just log a message here.
            self.ctx["log"].info(f"OK, all {self.ctx['csv'].get_row_count()} rows in the CSV file have the same number of columns as there are headers ({len(self.ctx['csv'].field_names)}).")


        common_checks_before_task(self.ctx, self.is_use_csv)
        self.general_checks()
        if self.is_use_csv:
            self.validate_required_fields()

        parent_id_checker = ParentPrecedesChildCheck(self.ctx["config"]["id_field"])
        row_counter = 0
        for row in self.rows():
            row_counter += 1
            check_row_field_count(self.ctx, row, row_counter)
            self.check_row(row, row_counter)
            parent_id_checker.visit(row_counter, row)
        parent_id_checker.finish(self.ctx)

        common_checks_after_task(self.ctx)
        self.finish()

    def prepare(self):
        """Prepare the task for execution."""
        try:
            check_for_required_config_keys(
                self.config_keys, self.required_fields, throw_exception=True
            )
        except KeyError as e:
            self.ctx["log"].error(f"Missing required configuration key: {e}")
            raise WorkbenchException(f"Missing required configuration key: {e}")

        if not self.is_check_mode:
            if self.ctx["config"]["remind_user_to_run_check"] is True and self.ctx["config"][
                "skip_user_prompts"] is not True:
                user_has_run_check = input("Have you run --check? (y/n)")
                if user_has_run_check.strip().lower() != "y":
                    self.ctx["log"].info(
                        'User was prompted whether they ran --check, and they responded "n". Exiting.', log_only=True
                    )
                    raise WorkbenchException("Your Workbench configuration file indicates you were to be reminded to run --check.")
            self.check_lock_file_setup()

        create_temp_dir(self.ctx["config"])

        if "user_prompts" in self.ctx["config"] and ("skip_user_prompts" not in self.ctx["config"] or self.ctx["config"]["skip_user_prompts"] is not True):
            prompt_user(self.ctx)

        if self.ctx["config"]["secondary_tasks"] is not None and len(self.ctx["config"]["secondary_tasks"]) > 0:
            secondary_tasks = []
            for secondary_config_file in self.ctx["config"]["secondary_tasks"]:
                secondary_tasks.append(os.path.abspath(secondary_config_file))
            os.environ["ISLANDORA_WORKBENCH_SECONDARY_TASKS"] = json.dumps(secondary_tasks)
            # We can't use the temp_dir to cache this list since the secondary tasks may not use
            # the same temp_dir as the primary task.
            os.environ["ISLANDORA_WORKBENCH_PRIMARY_TASK_TEMP_DIR"] = os.path.abspath(
                self.ctx["config"]["temp_dir"]
            )

        ping_islandora(self.ctx["config"], print_message=False)
        check_integration_module_version(self.ctx["config"], log_success=False)

        if len(self.ctx["config"]["input_data_zip_archives"]) > 0 and self.ctx["config"]["nodes_only"] is not True:
            for input_data_zip_archive in self.ctx["config"]["input_data_zip_archives"]:
                if input_data_zip_archive.lower().startswith("http"):
                    downloaded_zip_file_path = download_remote_archive_file(
                        self.ctx["config"], input_data_zip_archive
                    )
                    unzip_archive(self.ctx["config"], downloaded_zip_file_path)
                else:
                    unzip_archive(self.ctx["config"], input_data_zip_archive)

        if not self.ctx["config"]["nodes_only"] and not self.ctx["config"]["keep_filename_parent_directory"]:
            self.ctx["log"].info(
                "Parent directory names for files will not be used when creating Drupal media, but the directory names will appear in Workbench messages and log entries."
            )

        if self.ctx["config"]["enable_http_cache"] is True:
            if self.ctx["config"]["http_cache_storage"] == "sqlite" and os.path.exists("http_cache.sqlite"):
                self.ctx["log"].info('Cleared HTTP cache file "http_cache.sqlite".')
                os.remove("http_cache.sqlite")
            requests_cache.install_cache(
                backend=self.ctx["config"]["http_cache_storage"], expire_after=self.ctx["config"]["http_cache_storage_expire_after"], )
            message = "Client-side request caching is enabled."
        else:
            message = "Client-side request caching is not enabled."
        self.ctx["log"].info(message)

        check_drupal_core_version(self.ctx)


    def finish(self):
        """Summary, post-run output."""
        pass

    def run(self):
        """shared lifecycle; also owns progress bar and log lines"""
        self.prepare()
        self.prepare_run()
        if self.is_use_csv:
            try:
                csv = CsvReader.open(self.ctx['config'])
            except FileNotFoundError as e:
                self.ctx["log"].error(str(e))
                raise WorkbenchException(str(e))
            self.ctx["csv"] = csv
        row_count = 0
        for row in self.rows():
            row_count += 1
            self.process_row(row, row_count)
        self.finish()

    def rows(self):
        """Generator for rows; defaults to using a CSV file."""
        if self.is_use_csv:
            csv = self.ctx["csv"]
            for row in csv.rows():
                yield row
        else:
            yield {}

    @abstractmethod
    def general_checks(self):
        """General task checks, run once per task; each task overrides this."""
        pass

    @abstractmethod
    def check_row(self, row: Union[dict, str], row_number: int):
        """Per-row checks; each task overrides this.
        :param row: The row to check, either a dictionary (if using CSV) or a string (if not using CSV).
        :param row_number: The row number (1-based) for logging purposes.
        """
        pass

    @abstractmethod
    def prepare_run(self):
        """Prepare the run, run once per task; each task can override this."""
        pass

    @abstractmethod
    def process_row(self, row: Union[dict, str], row_number: int):
        """The per-row work; each task overrides this.
        :param row: The row to process, either a dictionary (if using CSV) or a string (if not using CSV).
        :param row_number: The row number (1-based) for logging purposes.
        """
        pass