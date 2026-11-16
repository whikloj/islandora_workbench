import os

from islandora_workbench.data_io.csv.csv_reader import CsvReader
from islandora_workbench.exceptions import WorkbenchValidationException
from workbench_utils import get_csv_id_to_node_id_map_allowed_hosts_sql, sqlite_manager


def validate_field_names(ctx: dict, csv_inner: CsvReader) -> None:
    """Validate the field names in the CSV header against the task requirements.

    :param ctx: dict - The context for this validation.
    :param csv_inner: CsvReader - The CSV reader object.
    :raises WorkbenchValidationException: If required field names are missing.
    """
    # Even though we check for the task's expected ID column in the incoming CSV in check_input(),
    # we need to check it here as well since check_input() reads the CSV prior to those checks.
    # TODO: Why can't these checks exist in check_input() only?
    id_columns = {"create": ctx["config"]["id_field"], "update": "node_id", "delete": "node_id", "add_media": "node_id",
        "delete_media": "media_id", "delete_media_by_node": "node_id", "update_media": "media_id",
        "update_media_by_node": "node_id", "create_terms": "term_name", "update_terms": "term_id",
        "export_csv": "node_id", }

    field_names = csv_inner.field_names
    if ctx["config"]["task"] in id_columns and id_columns[ctx["config"]["task"]] not in field_names:
        message = f'"{ctx["config"]["task"]}" tasks require a "{id_columns[ctx["config"]["task"]]}" CSV column. Please check your input CSV file and try again.'
        raise WorkbenchValidationException("Error: " + message)

    if ctx["config"]["task"] == "run_scripts":
        run_scripts_entity_type = ctx["config"]["run_scripts_entity_type"]
        id_columns = {"node": "node_id", "media": "media_id", "term": "term_id"}
        if id_columns[ctx["config"]["run_scripts_entity_type"]] not in field_names:
            message = f'Error: "run_scripts" tasks for "{run_scripts_entity_type}" entities require a "{id_columns[run_scripts_entity_type]}" CSV column. Please check your input CSV file and try again.'
            raise WorkbenchValidationException(message)

def check_for_duplicate_field_names(csv_inner: CsvReader) -> None:
    """Check for duplicate field names in the CSV header.

    :param csv_inner: CsvReader - The CSV reader object.
    :raises WorkbenchValidationException: If duplicate field names are found.
    """
    field_names = csv_inner.field_names
    seen = set()
    duplicates = []
    for item in field_names:
        if item not in seen:
            seen.add(item)
        else:
            duplicates.append(item)
    if len(duplicates) > 0:
        raise WorkbenchValidationException("Error: CSV has duplicate header names - " + ", ".join(duplicates))


def check_csv_headers_settings(ctx: dict) -> None:
    """Check that the CSV headers setting is compatible with the task.

    :param ctx: The context to use

    :raise WorkbenchValidationException: If the CSV headers setting is incompatible with the task.
    """
    label_tasks = ["create", "update", "create_terms", "update_terms"]
    if ctx["config"]["csv_headers"] == "labels" and ctx["config"]["task"] not in label_tasks:
        message = f"Error: CSV headers set to 'labels' but task is '{ctx['config']['task']}'. Must be one of {', '.join(label_tasks)}. Please check your configuration file and try again."
        raise WorkbenchValidationException(message)


def row_filter_checks(ctx: dict):
    """Perform checks on row filter settings.

    :param ctx: The context to use.
    """
    row_filter_settings = []
    if ctx["config"]["csv_start_row"] != 0:
        row_filter_settings.append("csv_start_row")
    if ctx["config"]["csv_stop_row"] is not None:
        row_filter_settings.append("csv_stop_row")
    if "csv_rows_to_process" in ctx["config"]:
        row_filter_settings.append("csv_rows_to_process")
    if "csv_row_filters" in ctx["config"]:
        row_filter_settings.append("csv_row_filters")
    if ctx["csv"].has_commented_out_rows is True:
        row_filter_settings.append(True)
    if "csv_start_row_skip" in ctx["config"]:
        row_filter_settings.append(True)
    if "csv_stop_row_skip" in ctx["config"]:
        row_filter_settings.append(True)
    if len(row_filter_settings) > 1:
        preprocessed_input_csv_file_path = ctx["csv"].path
        message = f'Your configuration contains more than one input CSV row filter setting, and/or your input CSV has some commented-out rows. Please check "{preprocessed_input_csv_file_path}" to confirm the rows you want are present.'
        ctx["log"].warning(message)


def csv_checks(ctx: dict) -> None:
    """Perform checks on the CSV headers and field names.
    :param ctx: The context to use.

    :return: None
    """
    csv = ctx["csv"]
    check_csv_headers_settings(ctx)
    check_for_duplicate_field_names(csv)
    validate_field_names(ctx, csv)
    row_filter_checks(ctx)

def check_row_field_count(ctx: dict, row: dict, row_number: int) -> None:
    """Check that the number of fields in the row matches the number of headers in the CSV.

    :param ctx: The context to use.
    :param row: The current row being checked.
    :param row_number: The current row number being checked.

    :raises WorkbenchValidationException: If the number of fields in the row does not match the number of headers.
    """
    if "stringtopopulateextrafields" in row.values():
        message = ("Row " + str(row_number) + " (ID " + row[
            ctx["config"]["id_field"]] + ") of the CSV file has fewer columns " + "than there are headers (" + str(
            len(ctx["csv"].field_names)
        ) + ").")
        ctx["log"].error(message)
        raise WorkbenchValidationException("Error: " + message)
    if len(row) != len(ctx["csv"].field_names):
        comparison = "fewer" if len(ctx["csv"].field_names) > len(row) else "more"
        message = ("Row " + str(row_number) + " (ID " + row[
            ctx["config"]["id_field"]] + ") of the CSV file has " + comparison + " columns (" + str(
            len(row)
        ) + ") than there are headers (" + str(len(ctx["csv"].field_names)) + ").")
        ctx["log"].error(message)
        raise WorkbenchValidationException("Error: " + message)

class ParentPrecedesChildCheck:
    """Class to check that lines that define a parent IDs in a CSV file precede their child ID lines."""

    def __init__(self, id_field):
        self.id_field = id_field  # The name of the field in the CSV that contains the unique identifier for each row.
        self.seen = {}          # dict of id -> row number of first occurrence
        self.pending = []       # list of tuples (row_number, row_id, parent_id)

    def visit(self, n: int, row: dict):
        row_id = row[self.id_field].strip()
        parent = row.get("parent_id", "").strip()
        if parent and parent not in self.seen:
            self.pending.append((n, row_id, parent))
        self.seen.setdefault(row_id, n)

    def finish(self, ctx: dict):
        errors = []
        for n, row_id, parent in self.pending:
            if parent in self.seen:
                # defined, but later in the file
                errors.append(f"Row {n} (ID {row_id}): parent {parent} first appears in row {self.seen[parent]}, after its child.")
                self.pending.remove((n, row_id, parent))
        if len(errors) > 0:
            raise WorkbenchValidationException("Error: " + "\n".join(errors))

        if ctx["config"]["query_csv_id_to_node_id_map_for_parents"] is True and ctx["config"][
                "csv_id_to_node_id_map_path"] is not False:

            if (len(ctx["config"]["csv_id_to_node_id_map_allowed_hosts"]) > 0 and ctx["config"][
                "query_csv_id_to_node_id_map_for_parents"] is True):
                csv_id_to_node_id_map_allowed_hosts_message = "Limiting"
            else:
                csv_id_to_node_id_map_allowed_hosts_message = "Not limiting"
            csv_id_to_node_id_map_allowed_hosts_message += (
                    f' lookups to those with the hostname "' + ctx["config"]["host"] + '".')

            message = f"Validating parent IDs in the CSV ID to node ID map, please wait. {csv_id_to_node_id_map_allowed_hosts_message}"
            ctx["log"].info(message)

            for n, row_id, parent in self.pending:
                parent_in_id_map_result = ctx["node_map"].get_node_id(parent)

                if isinstance(parent_in_id_map_result, list) and len(parent_in_id_map_result) > 1:
                    message = f'Row {n} (ID {row_id}): Query of ID map for parent ID "{parent}" returned multiple node IDs: ({", ".join([row["node_id"] for row in parent_in_id_map_result])}).'
                    ctx["log"].warning(message)
                elif parent_in_id_map_result is None:
                    message = f'Row {n} (ID {row_id}): Query of ID map for parent ID "{parent}" returned no node IDs.'
                    ctx["log"].warning(message)
