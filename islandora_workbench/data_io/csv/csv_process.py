import collections
import copy
import csv
import itertools
import json
import logging
import os
import random
import re
import string
import sys
import tempfile
import uuid
from typing import Union, OrderedDict

from cachetools import cached, LFUCache

from islandora_workbench.data_io.csv.csv_cache import fingerprint
from islandora_workbench.drupal.fields import get_field_definitions
from islandora_workbench.exceptions import WorkbenchCsvReaderException, WorkbenchException
from islandora_workbench.utils import value_is_numeric, execute_subprocess_script


def get_csv_row_filters(config: dict) -> Union[tuple[dict, dict], None]:
    """Get the CSV row filtering parameters from the config.

    :param config: dict - The configuration dictionary.
    :return: tuple | None -  a tuple of is and is not dictionaries or None if no filters are defined.
    :raise ValueError: If an invalid operator is found in the csv_row_filters configuration.
    """
    # Prepare any "csv_row_filters", which we apply to each row, below.
    if "csv_row_filters" in config and len(config["csv_row_filters"]) > 0:
        row_filters_is = dict()
        row_filters_isnot = dict()
        # Then populate the lists of filter values.
        for filter_config in config["csv_row_filters"]:
            filter_group = filter_config.split(":")
            # Prepare the '' filter value.
            if filter_group[2] == "''" or filter_group[2] == '""':
                filter_group[2] = ""
            if filter_group[1] == "is":
                filter_group_field = filter_group[0]
                filter_group_value = filter_group[2]
                if filter_group_field not in row_filters_is.keys():
                    row_filters_is[filter_group_field] = []
                row_filters_is[filter_group_field].append(
                    filter_group_value.strip()
                )
            elif filter_group[1] == "isnot":
                filter_group_field = filter_group[0]
                filter_group_value = filter_group[2]
                if filter_group_field not in row_filters_isnot.keys():
                    row_filters_isnot[filter_group_field] = []
                row_filters_isnot[filter_group_field].append(
                    filter_group_value.strip()
                )
            else:
                raise ValueError(f'Invalid csv_row_filters operator "{filter_group[1]}" in config file; must be "is" or "isnot".')
        return row_filters_is, row_filters_isnot
    return None

def get_csv_rows_to_process(config: dict) -> list:
    """Get the list of IDs to skip from the config, if any.
    :param config: dict - The configuration dictionary.
    :return: list - A list of IDs to skip.
    :raise FileNotFoundError: If the file specified in the "csv_rows_to_process" config setting cannot be found.
    """
    # If the value in config['csv_rows_to_process'] is a path to a file, skip rows not identified in the file.
    ids_to_process = []
    if "csv_rows_to_process" in config:
        if isinstance(config["csv_rows_to_process"], list):
            ids_to_process = [str(x) for x in config["csv_rows_to_process"]]
        elif isinstance(config["csv_rows_to_process"], str):
            path_to_ids_file = os.path.abspath(config["csv_rows_to_process"])
            if os.path.exists(path_to_ids_file) and os.path.isfile(path_to_ids_file):
                with open(path_to_ids_file) as fh:
                    ids_to_process = fh.read().splitlines()
                    ids_to_process = [x for x in ids_to_process if x]
            else:
                message = f'File identified in the "csv_rows_to_process" config setting ({path_to_ids_file}) cannot be found.'
                raise FileNotFoundError(message)
    return ids_to_process

def get_csv_rows_to_skip(config: dict) -> list:
    """Get the list of row IDs to skip from the config, if any.
    :param config: dict - The configuration dictionary.
    :return: list - A list of row IDs to skip.
    :raise WorkbenchCsvReaderException: If the "csv_start_row_skip" and "csv_stop_row_skip" config settings are not both defined.
    """
    start_skip = config.get("csv_start_row_skip", None)
    stop_skip = config.get("csv_stop_row_skip", None)

    if start_skip is None and stop_skip is None:
        return []

    if (start_skip is not None and stop_skip is None) or (start_skip is None and stop_skip is not None):
        raise WorkbenchCsvReaderException('Both "csv_start_row_skip" and "csv_stop_row_skip" must be defined in the config file if either is defined.')

    if not value_is_numeric(start_skip) or not value_is_numeric(stop_skip):
        raise WorkbenchCsvReaderException('"csv_start_row_skip" and "csv_stop_row_skip" must be numeric values in the config file.')

    start_skip = int(start_skip)
    stop_skip = int(stop_skip)

    if start_skip > stop_skip:
        raise WorkbenchCsvReaderException('"csv_start_row_skip" must be less than or equal to "csv_stop_row_skip" in the config file.')

    return list(range(start_skip, stop_skip + 1))


def write_preprocessed_csv(ctx: dict, source_file_path: str, cache_path: str) -> dict[str, Union[int, str]]:
    """Read the input CSV data and prepare it for use in all tasks that use an input CSV file.

    This function reads the source CSV file (or the CSV dump from Google Sheets or Excel),
    applies some prepocessing to each CSV record (specifically, it adds any CSV field
    templates that are registered in the config file, and it filters out any CSV
    records or lines in the CSV file that begine with a #), and finally, writes out
    a version of the CSV data to a file that appends .preprocessed to the input
    CSV file name. It is this .preprocessed file that is used in create, update, etc.
    tasks.

    :param ctx: dict - The context dictionary.
    :param source_file_path: str - The path to the source CSV file.
    :param cache_path: str - The path to the cache file where the preprocessed CSV will be written.
    :return: dict - A dictionary containing metadata about the preprocessed CSV,
    :raise FileNotFoundError: If the source file or the file specified in the "csv_rows_to_process" config setting cannot be found.
    """
    metadata = {}
    config = copy.deepcopy(ctx['config'])

    try:
        # 'utf-8-sig' encoding skips Microsoft BOM (0xef, 0xbb, 0xbf) at the start of files,
        # e.g. exported from Excel and has no effect when reading standard UTF-8 encoded files.
        with open(
            source_file_path, "r", encoding="utf-8-sig", newline=""
        ) as csv_reader_file_handle:
            # The CSV reader argument 'restval' is used to populate superfluous fields/labels.
            csv_reader = csv.DictReader(
                csv_reader_file_handle,
                delimiter=config["delimiter"],
                restval="stringtopopulateextrafields",
            )

            # Just get the fieldnames from the CSV, no validation yet.
            if config["csv_headers"] == "labels":
                csv_reader_fieldnames = replace_field_labels_with_names(
                    config, list(csv_reader.fieldnames)
                )
            else:
                csv_reader_fieldnames = list(csv_reader.fieldnames)

            if config["task"] == "update_media_by_node":
                csv_reader_fieldnames.append("media_id")

            #  Remove any columns the user has configured to ignore.
            csv_reader_fieldnames = [
                x
                for x in csv_reader_fieldnames
                if x not in config["ignore_csv_columns"]
            ]

            #  If configured to do so, add "field_viewer_override" to output CSV so we can autopopulate the field_viewer_override column.
            if ("field_viewer_override" not in csv_reader_fieldnames and
                (
                    "field_viewer_override_extensions" in config
                    or "field_viewer_override_models" in config
                )
            ):
                csv_reader_fieldnames.append("field_viewer_override")

            # Common preprocessing tasks for the input CSV

            # We subtract 1 from config['csv_start_row'] so user's expectation of the actual
            # start row match up with Python's 0-based counting.
            if config["csv_start_row"] > 0:
                csv_start_row = config["csv_start_row"] - 1
            else:
                csv_start_row = config["csv_start_row"]

            try:
                ids_to_process = get_csv_rows_to_process(config)
            except FileNotFoundError as e:
                ctx['log'].error(e.strerror)
                raise e

            # If the config file contains CSV field templates, append them to the CSV data.
            # Make a copy of the column headers so we can skip adding templates to the new CSV
            # if they're present in the source CSV. We don't want fields in the source CSV to be
            # stomped on by templates.
            csv_field_template_field_names = []
            if "csv_field_templates" in config:
                for template in config["csv_field_templates"]:
                    for field_name, field_value in template.items():
                        if field_name not in csv_reader_fieldnames:
                            csv_reader_fieldnames.append(field_name)
                            csv_field_template_field_names.append(field_name)

            # Prepare any "csv_row_filters", which we apply to each row, below.
            row_filters_is, row_filters_isnot = get_csv_row_filters(config)

            csv_skip_row_numbers = get_csv_rows_to_skip(config)

            try:
                # Use a temporary file to write the preprocessed CSV data, then move it to the cache path when done. This way we avoid leaving a partially written cache file if something goes wrong during preprocessing.
                with tempfile.NamedTemporaryFile(
                    prefix="workbench_tmp_",
                    suffix=".csv",
                    mode="w",
                    encoding="utf-8",
                    newline="",
                    delete=False,
                ) as csv_writer_file_handle:
                    temp_path = csv_writer_file_handle.name
                    csv_writer = csv.DictWriter(
                        csv_writer_file_handle,
                        fieldnames=csv_reader_fieldnames,
                        delimiter=config["delimiter"],
                    )
                    csv_writer.writeheader()
                    row_num = 0

                    commented_out_input_csv_rows_present = False
                    if csv_start_row > 0:
                        ctx["log"].info("Starting to read CSV file at row " + str(csv_start_row + 1))
                    if config["csv_stop_row"] is not None:
                        ctx["log"].info("Stopping reading CSV file at row " + str(config["csv_stop_row"]))
                    for row in itertools.islice(
                        csv_reader, csv_start_row, config["csv_stop_row"]
                    ):

                        # Skip CSV records whose first column begins with #.
                        if str(list(row.values())[0]).strip().startswith("#"):
                            # Use new instance variable instead of a global, this is only read once.
                            commented_out_input_csv_rows_present = True
                            continue

                        # Skip CSV records whose row numbers are in the list of rows to skip.
                        if row_num in csv_skip_row_numbers:
                            ctx["log"].info(f"Skipping row {str(row_num + 1)}, (csv_start_row_skip - csv_stop_row_skip)")
                            continue

                        # If the config file contains a list of IDs to process, skip any rows that don't match.
                        try:
                            if (
                                len(ids_to_process) > 0
                                and row[config["id_field"]]
                                not in ids_to_process
                            ):
                                ctx["log"].info(f"Skipping row {str(row_num + 1)}, (csv_rows_to_process)")
                                continue
                        except UnboundLocalError:
                            # If there is no csv_rows_to_process key in the config the ids_to_process would not be defined
                            pass

                        # Apply the "is" and "isnot" csv_row_filters defined above. If the field/value
                        # combo is in the 'isnot' list, skip this row.
                        try:
                            filter_out_this_csv_row = False
                            if len(row_filters_isnot) > 0:
                                for (
                                    filter_field,
                                    filter_values,
                                ) in row_filters_isnot.items():
                                    if (
                                        len(filter_values) > 0
                                        and filter_field in row
                                    ):
                                        # Split out multiple field values to test each one.
                                        values_in_row_field = [
                                            x.strip()
                                            for x in row[filter_field].split(
                                                config["subdelimiter"]
                                            )
                                        ]
                                        for (
                                            value_in_row_field
                                        ) in values_in_row_field:
                                            if value_in_row_field in filter_values:
                                                filter_out_this_csv_row = True
                                                break
                                    if filter_out_this_csv_row is True:
                                        # Need to break out from the outer loop too if we are skipping.
                                        break
                            if filter_out_this_csv_row is True:
                                ctx["log"].info(f"Skipping row {str(row_num + 1)}, (csv_row_filters 'is')")
                                continue

                            # If the field/value combo is not in the 'is' list, skip this row.
                            if len(row_filters_is) > 0:
                                for (
                                    filter_field,
                                    filter_values,
                                ) in row_filters_is.items():
                                    filter_out_this_csv_row = (
                                        True  # Must match each filter or we skip
                                    )
                                    if (
                                        len(filter_values) > 0
                                        and filter_field in row
                                    ):
                                        # Split out multiple field values to test each one.
                                        values_in_row_field = [
                                            x.strip()
                                            for x in row[filter_field].split(
                                                config["subdelimiter"]
                                            )
                                        ]
                                        for (
                                            value_in_row_field
                                        ) in values_in_row_field:
                                            if (
                                                value_in_row_field.strip()
                                                in filter_values
                                            ):
                                                filter_out_this_csv_row = False  # matched so we can proceed to the next filter
                                                break
                                if filter_out_this_csv_row is True:
                                    ctx["log"].info(f"Skipping row {str(row_num + 1)}, (csv_row_filters 'isnot')")
                                    continue
                        except UnboundLocalError:
                            # row_filters_is and row_filters_isnot might not have been defined, in which case we skip this.
                            pass

                        # All rows that make it this far are valid, so increment the row count.
                        row_num += 1

                        # Remove columns specified in config['ignore_csv_columns'].
                        if len(config["ignore_csv_columns"]) > 0:
                            for column_to_ignore in config[
                                "ignore_csv_columns"
                            ]:
                                if column_to_ignore in row:
                                    del row[column_to_ignore]

                        if "csv_field_templates" in config:
                            for template in config["csv_field_templates"]:
                                for field_name, field_value in template.items():
                                    if field_name in csv_field_template_field_names:
                                        # Only overwrite templates fields.
                                        row[field_name] = field_value

                        if (
                            "csv_value_templates" in config
                            and len(config["csv_value_templates"]) > 0
                        ):
                            row = apply_csv_value_templates(
                                config, "csv_value_templates", row
                            )

                        row = clean_csv_values(config, row)
                        csv_writer.writerow(row)



                # Move the temp file to the cache path, overwriting if it exists.
                os.replace(temp_path, cache_path)
                metadata['row_count'] = row_num
                metadata['field_names'] = csv_reader_fieldnames
                metadata['has_commented_out_rows'] = commented_out_input_csv_rows_present
                metadata['fingerprint'] = fingerprint(ctx['config'], source_file_path)
            finally:
                # If we raised an Exception before completing the temp file will exist, so clean it up.
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        return metadata
    except UnicodeDecodeError:
        message = (
            "CSV file " + source_file_path + " must be encoded in ASCII or UTF-8."
        )
        ctx['log'].error(message)
        raise WorkbenchCsvReaderException(f"Error: {message}")


def clean_csv_values(config: dict, row: dict) -> dict:
    """Performs basic string cleanup on CSV values. Applies to entire value,
    not each subdivided value.
        Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        row : OrderedDict
            A CSV row.
        Returns
        -------
        preprocessed_csv_reader
            The CSV dict object.
    """
    for field in row:
        if "smart_quotes" not in config["clean_csv_values_skip"]:
            # Replace smart/curly quotes with straight ones.
            row[field] = str(row[field]).replace("“", '"').replace("”", '"')
            row[field] = str(row[field]).replace("‘", "'").replace("’", "'")

        if "inside_spaces" not in config["clean_csv_values_skip"]:
            # Remove multiple spaces within string.
            row[field] = re.sub(" +", " ", str(row[field]))

        # Any outside .strip()s should come after 'outside_spaces', so they are removed first.
        # Assumes that spaces/newlines are the most likely extraneous leading and trailing
        # characters in CSV values.
        if "outside_spaces" not in config["clean_csv_values_skip"]:
            # Strip leading and trailing whitespace, including newlines.
            row[field] = str(row[field]).strip()

        if "outside_subdelimiters" not in config["clean_csv_values_skip"]:
            # Strip leading and trailing subdelimters.
            row[field] = str(row[field]).strip(config["subdelimiter"])

    return row


def apply_csv_value_templates(
    config: dict, template_config_setting: str, row: dict
) -> dict:
    """Applies templates to values in a CSV row. Template variables available are: $csv_value, $file,
    $filename_without_extension, $weight, $random_alphanumeric_string, $random_number_string, and
    $uuid_string.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        template_config_setting: str
            The config setting to get the templates from. One of 'csv_value_templates' or
            'csv_value_templates_for_paged_content'.
        row: dict
            A CSV row to apply the template(s) to. For pages/children created from subdirectories, this
            is a version of the parent's row so we can get $csv_value values for non-required fields.
        Returns
        -------
        dict
            The row with CSV value templates applied.
    """
    templates = dict()
    for template in config[template_config_setting]:
        for field_name, value_template in template.items():
            templates[field_name] = value_template

    for field in row:
        if field in templates:
            incoming_subvalues = row[field].split(config["subdelimiter"])
            outgoing_subvalues = []
            for subvalue in incoming_subvalues:
                alphanumeric_string = "".join(
                    random.choices(
                        string.ascii_letters + string.digits,
                        k=config["csv_value_templates_rand_length"],
                    )
                )
                number_string = "".join(
                    random.choices(
                        string.digits, k=config["csv_value_templates_rand_length"]
                    )
                )
                uuid_string = str(uuid.uuid4())

                if "file" in row:
                    row_file_value = row["file"]
                else:
                    row_file_value = ""

                if "file" in row:
                    path, extension = os.path.splitext(row["file"])
                    filename_without_extension = os.path.basename(path)
                else:
                    filename_without_extension = ""

                if "field_weight" in row:
                    weight = row["field_weight"]
                else:
                    weight = ""

                if len(subvalue) > 0:
                    field_template = string.Template(templates[field])
                    subvalue = str(
                        field_template.substitute(
                            {
                                "csv_value": subvalue,
                                "file": row_file_value,
                                "filename_without_extension": filename_without_extension,
                                "weight": weight,
                                "random_alphanumeric_string": alphanumeric_string,
                                "random_number_string": number_string,
                                "uuid_string": uuid_string,
                            }
                        )
                    )
                    outgoing_subvalues.append(subvalue)

                # Handle empty CSV values, first for parent-level items and then for page/child items from
                # subdirectories (which will always have empty CSV values except for required fields).
                if len(row[field]) == 0:
                    if (
                        template_config_setting == "csv_value_templates"
                        and field in config["allow_csv_value_templates_if_field_empty"]
                    ):
                        field_template = string.Template(templates[field])
                        subvalue = str(
                            field_template.substitute(
                                {
                                    "csv_value": subvalue,
                                    "file": row_file_value,
                                    "filename_without_extension": filename_without_extension,
                                    "weight": weight,
                                    "random_alphanumeric_string": alphanumeric_string,
                                    "random_number_string": number_string,
                                    "uuid_string": uuid_string,
                                }
                            )
                        )
                        outgoing_subvalues.append(subvalue)

                    elif (
                        template_config_setting
                        == "csv_value_templates_for_paged_content"
                    ):
                        field_template = string.Template(templates[field])
                        subvalue = str(
                            field_template.substitute(
                                {
                                    "csv_value": subvalue,
                                    "file": row_file_value,
                                    "filename_without_extension": filename_without_extension,
                                    "weight": weight,
                                    "random_alphanumeric_string": alphanumeric_string,
                                    "random_number_string": number_string,
                                    "uuid_string": uuid_string,
                                }
                            )
                        )
                        outgoing_subvalues.append(subvalue)

            templated_string = config["subdelimiter"].join(outgoing_subvalues)
            row[field] = templated_string

    return row


def preprocessor_scripts_csv_row(
    ctx: dict, row: dict, field: str, path_to_script: str
) -> str:
    """Execute field preprocessor scripts, if any are configured. Note that these scripts
    are applied to the entire value from the CSV field and not split field values,
    e.g., if a field is multivalued, the preprocesor must split it and then reassemble
    it back into a string before returning it. Note that preprocessor scripts work only
    on string data and not on binary data like images, etc. and only on custom fields
    (so not title).
    Parameters
    :param ctx: dict - The context for this task, including the configuration settings and logger.
    :param row: dict - The CSV row being processed.
    :param field: string - The field in the CSV row being preprocessed.
    :param path_to_script: string - The absolute path to the preprocessor script.
    :return: string - The preprocessed field data or the original field data if the preprocessor failed.
    """
    output, return_code = preprocessor_scripts_field_data(ctx["config"], row[field], path_to_script)
    if return_code == 0:
        preprocessor_input = copy.deepcopy(row[field])
        ctx["log"].info(
            f'Preprocess command {path_to_script} executed, taking "{preprocessor_input}" as input and returning "{output.strip()}".'
        )
        return output.strip()
    else:
        message = f"Preprocess command {path_to_script} failed with return code {return_code}."
        ctx["log"].error(message)
        return row[field]


def preprocessor_scripts_field_data(config, field_value: str, path_to_script: str) -> tuple:
    """Executes a field preprocessor script and returns its output and exit status code. The script
    is passed the field subdelimiter as defined in the config YAML and the field's value, and
    prints a modified vesion of the value (result) back to this function.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_value: str - The field value to preprocess. Can be ''.
    :param path_to_script: str - The absolute path to the preprocessor script.
    :return: tuple - The output of the script (stdout) and the script's return code.
    """
    return execute_subprocess_script(config, path_to_script, field_value=field_value)


def replace_field_labels_with_names(config: dict, csv_headers: list) -> list:
    """Replace field labels in a list of CSV column headers with their machine name equivalents.

    Note: we can't use this feature for add_media or update_media tasks since the fieldnames
    vary by media type, and each row in the CSV can have a different media type.

     Parameters
     ----------
     config : dict
         The configuration settings defined by workbench_config.get_config().
     csv_headers: list
         A list containing the CSV headers.
     Returns
     -------
     list
         The list of CSV headers with any labels replaced with field names.
    """
    if config["task"] == "create_terms" or config["task"] == "update_terms":
        field_map = get_fieldname_map(
            config, "taxonomy_term", config["vocab_id"], "labels"
        )
    else:
        field_map = get_fieldname_map(config, "node", config["content_type"], "labels")

    for header_index in range(len(csv_headers)):
        if csv_headers[header_index] in field_map:
            csv_headers[header_index] = field_map.get(csv_headers[header_index])

    return csv_headers


@cached(
   cache=LFUCache(maxsize=100),
    key=lambda config, entity_type, bundle_type, keys, die: f"{entity_type}-{bundle_type}-{keys}",
)
def get_fieldname_map(
    config: dict, entity_type: str, bundle_type: str, keys: str, die: bool = True
) -> Union[dict, bool]:
    """Get a mapping of field machine names to labels, or labels to machine names.

    Note: does not account for multilingual configurations.

     Parameters
     ----------
     config : dict
         The configuration settings defined by workbench_config.get_config().
     entity_type : string
         One of 'node', 'media', 'taxonomy_term', or 'paragraph'.
     bundle_type : string
         The node content type, the vocabulary name, or the media type
         (image', 'document', 'audio', 'video', 'file', etc.).
     keys: string
         One of 'labels' or 'names'. 'labels' returns a dictionary where the field labels are
         the keys, 'names' returns a dictionary where the field machine names are the keys.
     die: bool
         Whether or not to exit if there is a problem generating the map.
     Returns
     -------
     dict|bool
         A dictionary with either field labels or machine names as the keys.
         Returns False if the field labels are not unique.
    """
    # We delete the cached map in check_input() and in Workbench's create(), update(), and
    # create_terms() functions so the cache is always fresh.
    field_defs = get_field_definitions(config, entity_type, bundle_type)
    label_map = dict()
    labels = []
    for field, properties in field_defs.items():
        labels.append(properties["label"])
        if keys == "labels":
            label_map[properties["label"]] = field

        if keys == "names":
            label_map[field] = properties["label"]

    if keys == "labels":
        duplicate_labels = [
            label for label, count in collections.Counter(labels).items() if count > 1
        ]
        if len(duplicate_labels) > 0:
            if die:
                message = (
                        f"Duplicate field labels exist ({', '. join(duplicate_labels)}). To continue, remove the \"csv_headers\" setting "
                        + "from your configuration file and change your CSV headers from field labels to field machine names."
                )
                raise WorkbenchException(message)
            else:
                return False

    return label_map
