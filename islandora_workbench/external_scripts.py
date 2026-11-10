import logging
import subprocess
from typing import Union

from islandora_workbench.drupal.client import get_mid_from_media_url_alias, get_tid_from_term_url_alias


def run_bootstrap_scripts(ctx: dict):
    """Runs the bootstrap scripts defined in the configuration.
    :param ctx: dict - The context dictionary containing configuration and logging information.
    """
    if "bootstrap" in ctx["config"] and len(ctx["config"]["bootstrap"]) > 0:
        for command in ctx["config"]["bootstrap"]:
            print("Executing bootstrap script " + command)
            output, return_code = execute_bootstrap_script(ctx["config"], command)
            if return_code == 0:
                ctx["log"].info(f"Bootstrap script {command} executed successfully.")
            else:
                ctx["log"].error(
                    f"Bootstrap script {command} failed with exit code {str(return_code)}."
                )

def execute_bootstrap_script(
    config, path_to_script: str
) -> tuple:
    """Executes a bootstrap script and returns its output and exit status code.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param path_to_script: str - The absolute path to the bootstrap script.
    :return: tuple - The output of the script (stdout) and the script's return code.
    """
    *interpreter, script = path_to_script.split(" ")
    cmd = subprocess.Popen(
        [*interpreter, script, config["config_file_path"]], stdout=subprocess.PIPE
    )
    result, stderrdata = cmd.communicate()
    result = result.decode().strip()

    if config["show_bootstrap_script_output"] is True:
        print(result)

    return result, cmd.returncode


def execute_script_to_run(
    config: dict, path_to_script: str, entity_type: str, entity_id: str
) -> Union[tuple, None]:
    """Executes a entity-level script and returns its output and exit status code.
    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    path_to_script : string
        The absolute path to the Workbench config file.
    entity_type: str
        One of "node", "media", or "term".
    entity_id: str
         The name of the CSV column containing the filename. None if the file isn't
         in a CSV field (e.g., when config['paged_content_from_directories'] is True or
         config["paged_content_from_directories_parents_exist"] is True).
    Returns
    -------
    str, str|None
        The output of the script (stdout or stderr) and the script's return code,
        or None if an exception within this function occurred.
    """
    if value_is_numeric(entity_id) is False:
        if entity_type == "node":
            entity_id = get_nid_from_url_alias(config, entity_id)
        if entity_type == "media":
            entity_id = get_mid_from_media_url_alias(config, entity_id)
        if entity_type == "term":
            entity_id = get_tid_from_term_url_alias(config, entity_id)

    try:
        result, stderrdata = execute_subprocess_script(config, path_to_script, config_file_path=config["config_file_path"], entity_id=entity_id)

        return result, cmd.returncode
    except Exception as e:
        message = f'Could not execute script "{path_to_script}": {e}'
        logging.error(message)
        return None


def execute_entity_post_task_script(
    path_to_script: str,
    path_to_config_file: str,
    http_response_code: int,
    entity_json: str = "",
    filename: str = "",
):
    """Executes a entity-level post-task script and returns its output and exit status code."""
    *interpreter, script = path_to_script.split(" ")
    cmd = subprocess.Popen(
        [
            *interpreter,
            script,
            path_to_config_file,
            str(http_response_code),
            entity_json,
            filename,
        ],
        stdout=subprocess.PIPE,
    )

    result, stderrdata = cmd.communicate()
    result = result.decode().strip()

    return result, cmd.returncode


def execute_shutdown_script(
    config, path_to_script: str, path_to_config_file: str
) -> tuple:
    """Executes a shutdown script and returns its output and exit status code.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param path_to_script: str - The absolute path to the shutdown script.
    :param path_to_config_file: str - The absolute path to the Workbench config file
    :return: tuple - The output of the script (stdout) and the script's return code.
    """
    result, stderrdata = execute_subprocess_script(config, path_to_script, path_to_config_file=path_to_config_file)

    if config["show_shutdown_script_output"] is True:
        print(result)
    return result, cmd.returncode
