import csv
from datetime import datetime
import json
import os
import string
import sys

from ruamel import yaml
from islandora_workbench.exceptions import WorkbenchException
from islandora_workbench.utils import value_is_numeric, get_config_file_identifier


class RollbackMgr:
    """Class to manage rollback operations for Workbench tasks."""

    def __init__(self, ctx: dict):
        """Initialize the RollbackMgr with the provided context.
        :param ctx: dict - The dictionary of configuration settings and logging.
        """
        self.config = ctx["config"]
        self.rollback_csv_filepath = get_rollback_csv_filepath(ctx)
        self.rollback_config_filepath = get_rollback_config_filepath(self.config)
        write_rollback_config(ctx, self.rollback_csv_filepath)
        prep_rollback_csv(self.config, self.rollback_csv_filepath)
        ctx["log"].info(f"Writing rollback CSV to {self.rollback_csv_filepath}.")

    def write_rollback_node_id(self, node_id: str, csv_id: str, node_title: str, node_file_path: str, member_of: str):
        """Write a row to the rollback CSV file with the provided node information.
        :param node_id - The node ID to write to the file.
        :param csv_id - The CSV ID to write to the file. Empty for pages created from directories,
            or for files used in create_from files tasks.
        :param node_title - The title of the node.
        :param node_file_path - The relative path to the value of the CSV "file" column, or for page/child
            nodes created from subdirectories, the path to the subdirectory and file.
        :param member_of - The value of "field_member_of" for the node.
        """
        if self.config["rollback_file_include_node_info"] is False:
            with open(self.rollback_csv_filepath, "a+") as rollback_csv_file:
                rollback_csv_file.write(str(node_id) + "\n")
        else:
            with open(
                    self.rollback_csv_filepath, "a+", newline="", encoding="utf-8"
            ) as rollback_csv_file:
                rollback_csv_writer = csv.DictWriter(
                    rollback_csv_file,
                    fieldnames=["node_id", self.config["id_field"], "title", "field_member_of", "file", ]
                )
                rollback_csv_writer.writerow(
                    {
                        "node_id": node_id,
                        self.config["id_field"]: csv_id,
                        "title": node_title,
                        "field_member_of": member_of,
                        "file": node_file_path,
                    }
                )


ROLLBACK_FMT_STRING = "%Y_%m_%d_%H_%M_%S"

def get_rollback_basename(ctx: dict, rollback_template_property: str) -> str:
    """Get the base name for the rollback file based on config settings and the specified template property.
    :param ctx: dict - The dictionary of configuration settings and logging.
    :param rollback_template_property: str - The name of the config property to use for the rollback filename template.
    :return: str - The base name for the rollback file.
    """
    config = ctx["config"]
    filename_basename = "rollback"

    if rollback_template_property in config:
        config_filename, task_config_ext = os.path.splitext(config["config_file"])
        input_csv_filename, input_csv_ext = os.path.splitext(config["input_csv"])

        rollback_filename_template = string.Template(config[rollback_template_property])
        try:
            if config["task"] == "create":
                filename_basename = str(
                    rollback_filename_template.substitute(
                        {"config_filename": config_filename, "input_csv_filename": input_csv_filename,
                            "csv_start_row": str(config["csv_start_row"]),
                            "csv_stop_row": str(config["csv_stop_row"]), }
                    )
                )
            elif config["task"] == "create_from_files":
                filename_basename = str(
                    rollback_filename_template.substitute(
                        {"config_filename": config_filename, }
                    )
                )
        except Exception as e:
            # We need to account for the very common case where the user has included "valid identifier characters"
            # (as defined in https://peps.python.org/pep-0292/) as part of their template. The most common case will
            # likely be underscores separating the template placeholders.
            message = f'One or more parts of the configured template ({rollback_template_property}) with value ({config[rollback_template_property]}) need adjusting.'
            ctx["log"].error(
                f"Error: {message} Please refer to your Workbench log and to the Workbench documentation for suggestions.",
                log_message=f"{message} A {e.__class__.__name__} exception occured with the error message {e}. Please refer to the Workbench documentation for suggestions."
            )
            raise WorkbenchException(f"Error: {message} Please refer to your Workbench log and to the Workbench documentation for suggestions.")

    return filename_basename

def get_rollback_filename(ctx: dict, basename: str, file_path_config_property: str) -> str:
    """Get the rollback filename based on config settings.
    Parameters
    :param ctx: dict - The dictionary of configuration settings and logging.
    :param basename: str - The base name of the rollback file.
    :param file_path_config_property: str - The name of the config property to use for the rollback file path.
    :return: str - The rollback filename.
    """
    config = ctx["config"]
    filepath_now_string = ""
    if config["timestamp_rollback"] or (config["recovery_mode_starting_from_node_id"] is not False and value_is_numeric(
        config["recovery_mode_starting_from_node_id"]
        )):
        filepath_now_string = datetime.fromtimestamp(ctx["EXECUTION_START_TIME"]).strftime(ROLLBACK_FMT_STRING)

    # If we get a specific path, use that.
    if file_path_config_property in config and len(config[file_path_config_property]) > 0:
        if config["timestamp_rollback"] is True:
            rollback_csv_file_path_head, rollback_csv_file_path_tail = os.path.split(
                config[file_path_config_property]
            )
            rollback_csv_file_basename, rollback_csv_file_ext = os.path.splitext(
                rollback_csv_file_path_tail
            )
            rollback_csv_file_path = os.path.join(
                rollback_csv_file_path_head,
                f"{rollback_csv_file_basename}.{filepath_now_string}{rollback_csv_file_ext}", )
            return os.path.abspath(rollback_csv_file_path)
        else:
            return os.path.abspath(config[file_path_config_property])

    # If we are running a secondary task, we need to append the config file identifier to the rollback CSV filename.
    if os.environ.get("ISLANDORA_WORKBENCH_SECONDARY_TASKS") is not None:
        secondary_tasks = json.loads(os.environ["ISLANDORA_WORKBENCH_SECONDARY_TASKS"])
        if os.path.abspath(config["current_config_file_path"]) in secondary_tasks:
            config_file_id = get_config_file_identifier(config)
            basename = f"{basename}.{config_file_id}"

    # If we are using a timestamped rollback, we need to append the timestamp to the rollback CSV filename.
    if config["timestamp_rollback"]:
        basename = f"{basename}.{filepath_now_string}"
    elif config["recovery_mode_starting_from_node_id"] is not False and value_is_numeric(
            config["recovery_mode_starting_from_node_id"]
            ):
        basename = f"{basename}.{filepath_now_string}.recovery_mode"

    rollback_dir = sys.path[0] if file_path_config_property not in config else config["rollback_dir"] or config["input_dir"]

    return os.path.abspath(
        os.path.join(
            rollback_dir, f"{basename}.csv"
        )
    )

def get_rollback_csv_filepath(ctx: dict) -> str:
    """Get the path to the rollback CSV file based on config settings.
    Parameters
    :param ctx: dict - The dictionary of configuration settings and logging.
    :return: str - The absolute path to the rollback CSV file.
    """
    rollback_csv_filename_basename = get_rollback_basename(ctx, "rollback_csv_filename_template")
    return get_rollback_filename(ctx, rollback_csv_filename_basename, "rollback_csv_file_path")

def get_rollback_config_filepath(ctx: dict) -> str:
    """Get the path to the rollback configuration file based on config settings.
    Parameters
    :param ctx: dict - The configuration settings and logging context.
    :return: str - The absolute path to the rollback configuration file.
    """
    filename_basename = get_rollback_basename(ctx, "rollback_config_filename_template")
    return get_rollback_filename(ctx, filename_basename, "rollback_config_file_path")

def write_rollback_config(ctx: dict, path_to_rollback_csv_file: str):
    """Writes a rollback configuration file to disk.
    Parameters
    :param ctx: dict - The configuration settings and logging context.
    :param path_to_rollback_csv_file: str - The path to the rollback CSV file.
    """
    config = ctx["config"]
    rollback_config_filename = get_rollback_config_filepath(config)

    ctx["log"].info(f"Writing rollback configuration file to {rollback_config_filename}.")
    rollback_config_file = open(rollback_config_filename, "w")
    rollback_comments = get_rollback_config_comments(config)
    rollback_config_file.write(rollback_comments)

    if config["include_password_in_rollback_config_file"] is True:
        password = config["password"]
    else:
        password = None

    output_config = {
        "task": "delete",
        "host": config["host"],
        "use_workbench_permissions": config["use_workbench_permissions"],
        "input_dir": config["input_dir"],
        "standalone_media_url": config["standalone_media_url"],
        "secure_ssl_only": config["secure_ssl_only"],
        "input_csv": path_to_rollback_csv_file,
    }
    if "credentials_file_path" in config:
        output_config["credentials_file_path"] = config["credentials_file_path"]
    else:
        output_config["username"] = config["username"]
        output_config["password"] = password

    with open(rollback_config_filename, "w") as rollback_config_file_fp:
        yaml.dump(output_config, rollback_config_file_fp)


def prep_rollback_csv(ctx: dict, path_to_rollback_csv_file: str):
    """Prepares the rollback CSV file by creating it and writing the header row.
    Parameters
    :param ctx: dict - The configuration settings and logging context.
    :param path_to_rollback_csv_file: str - The path to the rollback CSV file.
    """
    config = ctx["config"]
    try:
        if os.path.exists(path_to_rollback_csv_file):
            os.remove(path_to_rollback_csv_file)
        with open(path_to_rollback_csv_file, "a+") as rollback_csv_file_fp:
            if config["rollback_file_include_node_info"] is False:
                rollback_csv_file_fp.write("node_id\n")
            else:
                rollback_csv_file_fp.write(
                    f"node_id,{config['id_field']},title,field_member_of,file\n"
                )
            rollback_csv_comments = get_rollback_config_comments(config)
            rollback_csv_file_fp.write(rollback_csv_comments)
    except (PermissionError, OSError) as e:
        message = f"Workbench was unable save rollback CSV to {path_to_rollback_csv_file}. {str(e)}"
        ctx["log"].error(message)
        raise WorkbenchException(message)


def get_rollback_config_comments(config: dict) -> str:
    """Generates comments to add to the top of the rollback configuration file.
    Parameters
    :param config: dict - The configuration settings as defined by WorkbenchConfig.get_config().
    :return: str - The comments to add to the top of the rollback configuration file.
    """
    comments = list()
    task = config["task"]
    config_file = config["config_file"]
    input_csv = config["input_csv"]
    time_string = EXECUTION_START_TIME.strftime("%Y:%m:%d %H:%M:%S")

    comments.append(f'# Generated by a "{task}" task started {time_string} using')
    comments.append(f'config file "{config_file}" and input CSV "{input_csv}".')
    if (
        "rollback_file_comments" in config
        and config["rollback_file_comments"] is not None
        and len(config["rollback_file_comments"]) > 0
    ):
        comments.extend(config["rollback_file_comments"])

    return "\n# ".join(comments) + "\n"
