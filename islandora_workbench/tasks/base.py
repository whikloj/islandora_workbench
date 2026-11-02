from abc import abstractmethod, ABC

from islandora_workbench.checks.common import common_checks_before_task, common_checks_after_task
from islandora_workbench.checks.csv_checks import check_row_field_count, ParentPrecedesChildCheck, csv_checks
from islandora_workbench.data_io.csv_data import CsvReader
from islandora_workbench.exceptions import WorkbenchValidationException
from workbench_checks import _generate_geolocation_field_list, _generate_link_field_list, \
    _generate_authority_link_field_list, _generate_edtf_field_list, _generate_taxonomy_term_reference_field_list, \
    _generate_typed_relation_field_list, _generate_cardinality_field_list, _generate_max_length_field_list, \
    _generate_text_with_allowed_field_list, _generate_numeric_field_list, _generate_media_field_list
from workbench_utils import get_entity_reference_view_endpoints


class Task(ABC):
    """Base class for all tasks. Each task is a subclass of this class, and implements the methods below."""

    # task name
    name: str
    # context dictionary
    ctx: dict
    # configuration keys
    config_keys: list[str]
    # required fields
    required_fields: list[str] = []
    # ID field
    id_field: str
    # Does this task use a CSV file? (default: True)
    uses_csv: bool = True
    # Are we doing validation (--check)? (default: False)
    check_mode: bool = False
    # Field configurations
    field_configs: dict = {
        # list of fields that are valid for the entity type and bundle being processed.
        "entity_fields": [],
        # entity field definitions for the entity type and bundle being processed.
        "entity_field_definitions": {},
        # dictionary of additional files configuration for easy access.
        "additional_files": {},
        # dictionary of required fields for the entity type and bundle being processed.
        "required_bundle_fields": {},
        # media bundle field definitions
        "media_bundle_field_definitions": {},
        # list of base fields for nodes.
        "node_base_fields": ["title", "status", "promote", "sticky", "uid", "created", "published", ],
        # list of reserved fields that should not be used as entity fields.
        "reserved_fields": ["file", "directory", "media_use_tid", "checksum", "node_id", "url_alias", "image_alt_text",
        "parent_id", "langcode", "revision_log", ],
    }
    # Various field types to make validation of these fields that exist easier, see generate_field_lists()
    field_types: dict = {}
    # testing flags to allow passing checks run at general check level to the row_check level.
    testing_flags: dict = {}


    def __init__(self, ctx: dict):
        self.ctx = ctx
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
        self.field_types["entity_reference_view_endpoints"] = get_entity_reference_view_endpoints(self.ctx["config"]) if "entity_reference_view_endpoints" in self.ctx["config"] else {}


    def validate_required_fields(self, csv: CsvReader):
        """Validate the fields in the CSV against the task requirements.
        :param csv: CsvReader - The CSV reader object.
        :raises WorkbenchCsvReaderException: If required fields are missing.
        """
        csv_field_names = csv.field_names
        missing_fields = [field for field in self.required_fields if field not in csv_field_names]
        if missing_fields:
            message = f"Required fields {', '.join(missing_fields)} are missing from the CSV file. Please check your input CSV file and try again."
            self.ctx["log"].error(message)
            raise WorkbenchValidationException(message)


    def check(self):
        """Perform task-specific checks, called from checks.run_checks."""
        common_checks_before_task(self.ctx)

        self.general_checks()
        if self.uses_csv:
            csv = CsvReader.open(self.ctx['config'])
            self.ctx["csv"] = csv
            csv_checks(self.ctx)
            self.validate_required_fields(csv)
            self.general_csv_checks()
            parent_id_checker = ParentPrecedesChildCheck(self.ctx["config"]["id_field"])
            row_counter = 0
            for row in csv.rows():
                row_counter += 1
                check_row_field_count(self.ctx, row, row_counter)
                self.check_row(row, row_counter)
                parent_id_checker.visit(row_counter, row)
            parent_id_checker.finish()
        common_checks_after_task(self.ctx)

    def prepare(self):
        """Prepare the task for execution. (i.e. rollback files, ID map, csv load, etc.)"""
        pass

    @abstractmethod
    def general_checks(self):
        """The general checks; each task overrides this."""
        pass

    def general_csv_checks(self):
        """The general CSV checks (if necessary); each task CAN override this."""
        pass

    @abstractmethod
    def check_row(self, row: dict, row_number: int):
        """The per-row checks; each task overrides this."""
        pass

    @abstractmethod
    def process_row(self, row: dict, row_number: int):
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

