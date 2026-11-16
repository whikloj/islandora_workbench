import json
import os
import subprocess

from islandora_workbench.checks.csv_checks import csv_checks
from islandora_workbench.checks.validation import validate_input_dir
from islandora_workbench.exceptions import WorkbenchValidationException
from workbench_utils import ping_remote_file
from islandora_workbench.checks.internal import check_for_workbench_updates
from islandora_workbench.utils import get_file_hash_from_local
from islandora_workbench.drupal import ping_islandora


def input_data_zip_archives_checks(ctx: dict):
    """Check existence of input data zip archives.
    :param ctx: The context to use
    """
    if len(ctx["config"]["input_data_zip_archives"]) > 0:
        for input_data_zip_archive_location in ctx["config"]["input_data_zip_archives"]:
            if input_data_zip_archive_location.lower().startswith("http"):
                remote_zip_archive_ping_response_code = ping_remote_file(
                    ctx["config"], input_data_zip_archive_location
                )
                if remote_zip_archive_ping_response_code != 200:
                    message = f'Remote input data zip archive "{input_data_zip_archive_location}" not found, ping returned a {remote_zip_archive_ping_response_code} response.'
                    ctx["log"].warning(message)
            else:
                if os.path.exists(input_data_zip_archive_location):
                    message = f'Local input data zip archive "{input_data_zip_archive_location}" found.'
                    ctx["log"].info(message)
                else:
                    message = f'Local input data zip archive "{input_data_zip_archive_location}" not found.'
                    ctx["log"].warning(message)

def bootstrap_script_checks(ctx: dict):
    """Check for bootstrap scripts, if any are configured.

    :param ctx: The context to use
    """
    if "bootstrap" in ctx["config"] and len(ctx["config"]["bootstrap"]) > 0:
        for bootstrap_script in ctx["config"]["bootstrap"]:
            if " " in bootstrap_script:
                bootstrap_script = bootstrap_script.split(" ")[-1]
            if not os.path.exists(bootstrap_script) or not os.path.isfile(bootstrap_script):
                ctx["log"].error(f"Bootstrap script {bootstrap_script} not found.")
                raise WorkbenchValidationException(f"Bootstrap script {bootstrap_script} not found.")
            if not os.access(bootstrap_script, os.X_OK):
                ctx["log"].error(f"Bootstrap script {bootstrap_script} is not executable.")
                raise WorkbenchValidationException(f"Bootstrap script {bootstrap_script} is not executable.")
        ctx["log"].info("OK, registered bootstrap scripts found and executable.")

def preprocess_script_checks(ctx: dict):
    """Check for preprocessor scripts, if any are configured.

    :param ctx: The context to use
    """
    if "preprocessors" in ctx["config"] and len(ctx["config"]["preprocessors"]) > 0:
        for preprocessor_script in ctx["config"]["preprocessors"]:
            if " " in preprocessor_script:
                preprocessor_script = preprocessor_script.split(" ")[-1]
            for pkey, pvalue in preprocessor_script.items():
                field = pkey.strip()
                script_path = pvalue.strip()
                # Since in some cases script paths need to include the interpreter (e.g. "python /path/to/script"),
                # we only check for the existence of the second part of the path name.
                if " " in script_path:
                    script_path_parts = script_path.split(" ")
                    script_path = script_path_parts[-1]
                if not os.path.exists(script_path) and os.path.isfile(script_path):
                    ctx["log"].error(f'Preprocessor script "{script_path}" for field "{field}" not found.')
                    raise WorkbenchValidationException(f"Preprocessor script \"{script_path}\" for field \"{field}\" not found.")
                if not os.access(script_path, os.X_OK):
                    ctx["log"].error(f'Preprocessor script "{script_path}" for field "{field}" is not executable.')
                    raise WorkbenchValidationException(f"Preprocessor script \"{script_path}\" for field \"{field}\" is not executable.")
        ctx["log"].info(f"OK, registered preprocessor scripts found and executable.")


def shutdown_script_checks(ctx: dict):
    """Check for shutdown scripts, if any are configured.

    :param ctx: The context to use
    """
    if "shutdown" in ctx["config"] and len(ctx["config"]["shutdown"]) > 0:
        for shutdown_script in ctx["config"]["shutdown"]:
            if " " in shutdown_script:
                shutdown_script = shutdown_script.split(" ")[-1]
            if not os.path.exists(shutdown_script) or not os.path.isfile(shutdown_script):
                ctx["log"].error(f"shutdown script {shutdown_script} not found.")
                raise WorkbenchValidationException(f"Shutdown script {shutdown_script} not found.")
            if not os.access(shutdown_script, os.X_OK):
                ctx["log"].error(f"Shutdown script {shutdown_script} is not executable.")
                raise WorkbenchValidationException(f"Shutdown script {shutdown_script} is not executable.")
        ctx["log"].info("OK, registered shutdown scripts found and executable.")

def lock_file_checks(ctx: dict):
    """Perform check_lock_file_path checks.

    :param ctx: The context to use
    """
    if "check_lock_file_path" in ctx["config"]:
        with open(ctx["config"]["check_lock_file_path"], "a") as check_lock_file:
            config_file_md5 = get_file_hash_from_local(ctx["config"]["config_file_path"], "md5")
            check_lock_file.write(
                f'Check against {ctx["config"]["config_file_path"]} (md5 hash {config_file_md5}) OK'
            )
            ctx["log"].info(
                f"Writing --check lock file \"{ctx['config']['check_lock_file_path']}\"."
            )

def secondary_tasks_checks(ctx: dict):
    """Run check of the secondary tasks, if configured

    :param ctx: The context to use
    """
    if ctx["config"]["secondary_tasks"] is not None:
        for secondary_config_file in json.loads(
            os.environ["ISLANDORA_WORKBENCH_SECONDARY_TASKS"]
        ):
            ctx["log"].info(f"\nRunning --check using secondary configuration file \"{secondary_config_file}\"")
            cmd = [
                ctx["config"]["path_to_python"],
                ctx["config"]["path_to_workbench_script"],
                "--config",
                secondary_config_file,
                "--check",
            ]
            subprocess.check_output(cmd)


def do_contactsheet_work(ctx: dict):
    """Perform contactsheet generation if enabled.

    :param ctx: The context to use
    """
    #if contactsheet is True:  # todo: pass args.contactsheet into workflow.
    #    if os.path.isabs(ctx.config["contact_sheet_output_dir"]):
    #        contact_sheet_path = os.path.join(
    #            ctx.config["contact_sheet_output_dir"], "contact_sheet.htm"
    #        )
    #    else:
    #        contact_sheet_path = os.path.join(
    #            os.getcwd(), ctx.config["contact_sheet_output_dir"], "contact_sheet.htm"
    #        )
    #    generate_contact_sheet_from_csv(ctx.config)
    #    ctx.log.info(f"Contact sheet is at {contact_sheet_path}.")

def postprocess_script_checks(ctx: dict):
    """Perform checks for post-processing scripts.
    :param ctx: The context to use
    """
    post_action_scripts_configs = ["node_post_create", "node_post_update", "media_post_create", ]
    post_action_scripts_present = False
    for post_action_script_config in post_action_scripts_configs:
        if post_action_script_config in ctx["config"] and len(ctx["config"][post_action_script_config]) > 0:
            post_action_scripts_present = True
            for post_action_script in ctx["config"][post_action_script_config]:
                if " " in post_action_script:
                    post_action_script = post_action_script.split(" ")[-1]
                if not os.path.exists(post_action_script) or not os.path.isfile(post_action_script):
                    ctx["log"].error(f"Post-action script {post_action_script} not found.")
                    raise WorkbenchValidationException(f"Post-action script {post_action_script} not found.")
                if not os.access(post_action_script, os.X_OK):
                    ctx["log"].error(f"Post-action script {post_action_script} is not executable.")
                    raise WorkbenchValidationException(f"Post-action script {post_action_script} not executable.")
        if post_action_scripts_present is True:
            ctx["log"].info("OK, registered post-action scripts found and executable.")

def common_checks_before_task(ctx: dict, use_csv: bool, log_message: str):
    """Run common checks for all tasks before the task specific checks are run.

    :param ctx: The context to use
    :param use_csv: Whether to perform CSV checks
    :param log_message: The log message to display at the start of the checks
    """
    if log_message:
        ctx["log"].info(log_message)

    check_for_workbench_updates(ctx["context"]["config"])
    validate_input_dir(ctx["config"])
    input_data_zip_archives_checks(ctx)

    bootstrap_script_checks(ctx)
    shutdown_script_checks(ctx)
    preprocess_script_checks(ctx)
    if use_csv:
        csv_checks(ctx)


def common_checks_after_task(ctx: dict):
    """Run common checks for all tasks after the task specific checks are run.

    :param ctx: The context to use
    """
    if ctx["config"]["perform_soft_checks"] is True:
        ctx["log"].info("Configuration and input data appear to be valid.")
        ctx["log"].warning(
            'Warning: "perform_soft_checks" is enabled so you need to review your log for errors despite the "OK" reports above.'
        )
    else:
        ctx["log"].info(
            "Configuration and input data appear to be valid. However, you should review your Workbench log after running --check."
            )

    ctx["log"].info(
        f'Configuration checked for "{ctx["config"]["task"]}" task using config file "{ctx["config"]["config_file"]}", no problems found.'
    )

    lock_file_checks(ctx)
    do_contactsheet_work(ctx)
    secondary_tasks_checks(ctx)
    postprocess_script_checks(ctx)
