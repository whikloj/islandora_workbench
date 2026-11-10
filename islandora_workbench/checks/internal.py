"""Various checks of the internal workings of Islandora Workbench."""
import json
import logging
import os
import subprocess
import sys

import requests

from islandora_workbench.data_io.rollback import get_rollback_config_filepath, get_rollback_csv_filepath


def check_rollback_file_path_directories(config: dict) -> None:
    """Check that the directories for rollback and CSV file paths exist and are writable.
    :param config: dict - The configuration settings defined by workbench_config.get_config().
    """
    rollback_config_file_path = get_rollback_config_filepath(config)
    rollback_config_file_path_head, rollback_config_file_path_tail = os.path.split(
        rollback_config_file_path
    )
    if not os.access(rollback_config_file_path_head, os.W_OK):
        message = f'Directory "{rollback_config_file_path_head}" in the rollback configuration file path does not exist or is not writable.'
        logging.error(message)
        sys.exit("Error: " + message)

    if config["check"] is True:
        logging.info(
            f"Rollback configuration file will be written to {rollback_config_file_path}."
        )

    rollback_csv_file_path = get_rollback_csv_filepath(config)
    rollback_csv_file_path_head, rollback_csv_file_path_tail = os.path.split(
        rollback_csv_file_path
    )
    if not os.access(rollback_csv_file_path_head, os.W_OK):
        message = f'Directory "{rollback_csv_file_path_head}" in the rollback CSV file path does not exist or is not writable.'
        logging.error(message)
        sys.exit("Error: " + message)

    if config["check"] is True:
        logging.info(f"Rollback CSV file will be written to {rollback_csv_file_path}.")


def check_for_workbench_updates(config: dict) -> None:
    """Checks to see if the local copy of Workbench is up to date with the main branch in Github.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    """
    if config["check_for_workbench_updates"] is False:
        return

    # Get current local branch name.
    try:
        git_branch_cmd = ["git", "branch", "--show-current"]
        current_branch_name = subprocess.check_output(git_branch_cmd)
        current_branch_name = current_branch_name.decode().strip()
        if current_branch_name != "main":
            message = f'Workbench cannot check when your local "main" Git branch was last updated, since you are currently in the "{current_branch_name}" branch.'
            logging.warning(message)
            return
    except Exception as e:
        logging.error(f"Could not get current git branch name: {e}")
        return

    # Get last 30 commits in local branch. We use 30 because that's that page size of the Github API commits endpoint.
    try:
        git_log_cmd = [
            "git",
            "log",
            "--pretty=format:%h,%cd",
            "--date=format:%Y-%m-%d",
            "-30",
        ]
        git_log_cmd_output = subprocess.check_output(git_log_cmd)
        git_log_output = git_log_cmd_output.decode().strip()
        git_log_output_lines = git_log_output.splitlines()
        latest_git_log_entry = git_log_output_lines[0]
        latest_local_commit, latest_local_commit_date = latest_git_log_entry.split(",")
    except Exception as e:
        logging.error(f"Could not get local git log entries: {e}")
        return

    # Get the last 30 commits from the main branch in Github.
    try:
        github_main_commits_url = (
            "https://api.github.com/repos/mjordan/islandora_workbench/commits"
        )
        github_main_commits_response = requests.get(github_main_commits_url)
        github_main_commits_list = json.loads(github_main_commits_response.content)
        remote_main_branch_commits = list()
        for c in github_main_commits_list:
            short_sha = c["sha"][:7]
            remote_main_branch_commits.append(short_sha)
    except Exception as e:
        logging.error(f"Could not get remote main branch commits from Github: {e}")
        return

    # Get the position of the latest local commit in the remote list of commits,
    # and construct corresponding output for the user and the log.
    if latest_local_commit in remote_main_branch_commits:
        latest_local_commit_position_in_remote_main_branch_commits = (
            remote_main_branch_commits.index(latest_local_commit)
        )
        if latest_local_commit_position_in_remote_main_branch_commits > 0:
            # Get date of most recent remote commit and include it in the message.
            try:
                github_main_latest_commit_url = f"https://api.github.com/repos/mjordan/islandora_workbench/commits/{remote_main_branch_commits[0]}"
                github_main_latest_commit_response = requests.get(
                    github_main_latest_commit_url
                )

                github_main_latest_commit = json.loads(
                    github_main_latest_commit_response.content
                )
                github_main_latest_commit_date = github_main_latest_commit["commit"][
                    "author"
                ]["date"].split("T", 1)[0]
                message = f'Your version of Workbench is {latest_local_commit_position_in_remote_main_branch_commits} commits behind the "main" branch in Github, which was last updated {github_main_latest_commit_date}.'
                logging.warning(message)
            except Exception as e:
                logging.error(
                    f"Could not get information on most recent commit in remote main branch from Github: {e}"
                )
                return
        else:
            message = "Looks like your copy of Workbench is up to date."
            logging.info(message)
    else:
        message = f'Looks like your copy of Workbench is at least 30 commits behind the "main" branch in Github. Your copy appears to have been last updated {latest_local_commit_date}.'
        logging.info(message)
