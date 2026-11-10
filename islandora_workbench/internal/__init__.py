"""Internal checks and functions for Islandora Workbench."""
import logging
import os
import sys
from pathlib import Path

from islandora_workbench.drupal.client import get_integration_module_version
from islandora_workbench.utils.string_utils import convert_semver_to_number


def create_temp_dir(config: dict):
    """Creates the temporary directory if it doesn't already exist.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    """
    if os.path.exists(config["temp_dir"]):
        temp_dir_exists_message = "already exists"
        make_temp_dir = False
    else:
        temp_dir_exists_message = "does not exist, will create it"
        make_temp_dir = True

    if config["temp_dir"] == config["input_dir"]:
        logging.info(
            f"Using directory defined in the 'input_dir' config setting ({config['input_dir']}) as the temporary directory ({temp_dir_exists_message})."
        )
    else:
        logging.info(
            f"Using directory defined in the 'temp_dir' config setting ({config['temp_dir']}) as the temporary directory ({temp_dir_exists_message})."
        )

    if make_temp_dir is True:
        Path(config["temp_dir"]).mkdir(exist_ok=True)


def check_integration_module_version(config: dict, log_success: bool = True) -> None:
    """Verifies if the minimum required version of the workbench integration module is being used.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    log_success : bool
        Whether or not to log that the desired version of the module is installed
        on the remote Drupal. Lack of minimum version is always logged.
    Returns
    -------
    None
    """
    version = get_integration_module_version(config)

    integration_module_min_version = "1.0"
    log_message = f'Drupal must be running version {integration_module_min_version} or higher of the Islandora Workbench Integration module. ({config["host"]} is running version {version}).'

    #####################################################################################################################
    # Add logic here that maps a feature to a miniumum Integration module version, and produces a specific log message. #
    #####################################################################################################################
    if config["use_workbench_permissions"] is True:
        integration_module_min_version = "1.2"
        log_message = f'In order to use the "use_workbench_permissions" config setting, Drupal must be running version {integration_module_min_version} or higher of the Islandora Workbench Integration module ({config["host"]} is running version {version}).'
    #####################################################################################################################

    if version is False:
        message = f"Workbench cannot determine the Islandora Workbench Integration module's version number. It must be version {integration_module_min_version} or higher."
        logging.error(message)
        sys.exit("Error: " + message)
    else:
        version_number = convert_semver_to_number(version)
        minimum_version_number = convert_semver_to_number(
            integration_module_min_version
        )
        if version_number < minimum_version_number:
            console_message = f'The Islandora Workbench Integration module installed on {config["host"]} must be upgraded to version {integration_module_min_version}. See your Workbench log for more information.'
            logging.error(log_message)
            sys.exit("Error: " + console_message)
        else:
            if log_success is True:
                logging.info(
                    f'OK, Islandora Workbench Integration module installed on {config["host"]} is at version {version}.'
                )
