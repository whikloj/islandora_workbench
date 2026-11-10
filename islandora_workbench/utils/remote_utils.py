import logging
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import OrderedDict

import requests
import requests_cache

from islandora_workbench.drupal.utils import get_node_title_from_nid
from islandora_workbench.utils.string_utils import get_deduped_file_path

from islandora_workbench.utils.utils import get_extension_from_mimetype


def download_remote_archive_file(config: dict, remote_archive_url: str):
    """Downloads a remote Zip archive to the temp directory.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param remote_archive_url: string - The URL of the remote Zip archive.
    :return: string|bool - The path to the downloaded Zip archive, or False on error.
    """
    message = f'Downloading Zip archive "{remote_archive_url}"...'
    print(message)
    logging.info(message)
    sections = urllib.parse.urlparse(remote_archive_url)
    archive_filename = os.path.basename(sections.path)
    if archive_filename.lower().endswith(".zip") is False:
        archive_filename = archive_filename + ".zip"
    try:
        if config["secure_ssl_only"] is False:
            requests.packages.urllib3.disable_warnings()
        # Do not cache the responses for downloaded files in requests_cache
        with requests_cache.disabled():
            response = requests.get(
                remote_archive_url,
                allow_redirects=True,
                stream=True,
                verify=config["secure_ssl_only"],
            )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
        message = (
            "Workbench " + "timed out trying to reach "
            if isinstance(error, requests.exceptions.Timeout)
            else "cannot connect to "
            + sections.netloc
            + " while connecting to "
            + remote_archive_url
            + ". Please verify that URL and check your network connection."
        )
        logging.error(message)
        logging.error(error)
        print("Error: " + message)
        return False

    downloaded_file_path = os.path.join(config["temp_dir"], archive_filename)
    with open(downloaded_file_path, "wb+") as output_file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                output_file.write(chunk)

    return downloaded_file_path


def get_remote_file_extension(config: dict, file_url: str) -> str:
    """For remote files that have no extension, such as http://acme.com/islandora/object/some:pid/datastream/OBJ/download,
    assign an extension, with a leading dot. If the file has an extension, return it, also with dot.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param file_url: str - The URL of the remote file.
    :return: str - The file extension, with a leading dot.
    """
    # If the file has an extension, just return it.
    extension = os.path.splitext(file_url)[1]
    extension = extension.lstrip(".").lower()
    if len(extension) > 0:
        return "." + extension

    # If it doesn't have an extension, assign one based on its MIME type. Request's docs at
    # https://requests.readthedocs.io/en/latest/user/quickstart/#response-headers say that
    # headers can be accessed regardless of capitalization, but that's not the case (ha).
    try:
        head_response = requests.head(
            file_url, allow_redirects=True, verify=config["secure_ssl_only"]
        )
        mimetype = head_response.headers["Content-Type"]
        if mimetype is None:
            mimetype = head_response.headers["content-type"]
            if mimetype is None:
                message = f'Cannot reliably get MIME type of file "{file_url}" from remote server.'
                logging.error(message)
                sys.exit("Error: " + message)

        # In case servers return stuff beside the MIME type in Content-Type header.
        # Assumes they use ; to separate stuff and that what we're looking for is
        # in the first position.
        if ";" in mimetype:
            mimetype_parts = mimetype.split(";")
            mimetype = mimetype_parts[0].strip()
    except KeyError:
        mimetype = "application/octet-stream"

    extension_with_dot = get_extension_from_mimetype(config, mimetype)

    if extension_with_dot is None:
        message = f'Workbench does not recognize the MIME type "{mimetype}" received from the remote server for the file "{file_url}". '
        message = (
            message
            + 'You can assign an extension to this MIME type using the "mimetype_extensions" config setting.'
        )
        logging.error(message)
        sys.exit("Error: " + message)

    return extension_with_dot


def download_remote_file(
    config: dict, url: str, file_fieldname: str, node_csv_row: OrderedDict, node_id: str
):
    headers = {"User-Agent": config["user_agent"]}

    sections = urllib.parse.urlparse(url)
    try:
        cookies = {
            config["remote_file_cookie_name"]: config["remote_file_cookie_value"]
        }
        if config["secure_ssl_only"] is False:
            requests.packages.urllib3.disable_warnings()
        # Do not cache the responses for downloaded files in requests_cache
        with requests_cache.disabled():
            response = requests.get(
                url,
                allow_redirects=True,
                stream=True,
                verify=config["secure_ssl_only"],
                headers=headers,
                cookies=cookies,
            )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
        message = (
            "Workbench "
            + (
                "timed out trying to reach "
                if isinstance(error, requests.exceptions.Timeout)
                else "cannot connect to "
            )
            + sections.netloc
            + " while connecting to "
            + url
            + ". Please verify that URL and check your network connection."
        )
        logging.error(message)
        logging.error(error)
        print("Error: " + message)
        return False

    downloaded_file_path = get_preprocessed_file_path(
        config, file_fieldname, node_csv_row, node_id
    )
    with open(downloaded_file_path, "wb+") as output_file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                output_file.write(chunk)

    return downloaded_file_path


def get_preprocessed_file_path(
    config: dict,
    file_fieldname: str,
    node_csv_row: OrderedDict,
    node_id: str = None,
    make_dir: bool = True,
) -> str:
    """For remote/downloaded files (other than from providers defined in config['oembed_providers]),
    generates the path to the local temporary copy and returns that path. For local files or oEmbed URLs,
    just returns the value of node_csv_row['file'].
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        file_fieldname: string
            The name of the CSV column containing the filename.
        node_csv_row : OrderedDict
            The CSV row for the current item.
        node_id: string
            The node ID of the node being processed.
        make_dir: bool
            Whether or not to create a directory for the node's files.
        Returns
        -------
        string
            The path (absolute or relative) to the file.
    """
    file_path_from_csv = node_csv_row[file_fieldname].strip()
    if config["task"] == "add_media":
        config["id_field"] = "node_id"

    # Test whether file_path_from_csv is from one of the oEmbed providers
    # and if so, return it here.
    for oembed_provider in config["oembed_providers"]:
        for provider_url, mtype in oembed_provider.items():
            if file_path_from_csv.startswith(provider_url):
                return file_path_from_csv

    # It's a remote file.
    if file_path_from_csv.startswith("http"):
        if config["task"] == "add_media":
            subdir = os.path.join(
                config["temp_dir"],
                re.sub("[^A-Za-z0-9]+", "_", str(node_csv_row["node_id"])),
            )
        elif config["task"] in ["update_media", "update_media_by_node"]:
            subdir = os.path.join(
                config["temp_dir"],
                re.sub("[^A-Za-z0-9]+", "_", node_csv_row["media_id"]),
            )
        else:
            subdir = os.path.join(
                config["temp_dir"],
                re.sub("[^A-Za-z0-9]+", "_", node_csv_row[config["id_field"]]),
            )
        if make_dir:
            Path(subdir).mkdir(parents=True, exist_ok=True)

        if "check" in config.keys() and config["check"] is True:
            try:
                os.rmdir(subdir)
            except Exception as e:
                # This can happen if subdirectories from previous runs of Workbench exist.
                logging.warning(f'Subdirectory "{subdir}" could not be deleted: {e}.')

        remote_extension_with_dot = get_remote_file_extension(
            config, file_path_from_csv
        )
        remote_filename_parts = os.path.splitext(file_path_from_csv)

        if (
            "use_node_title_for_remote_filename" in config
            and config["use_node_title_for_remote_filename"] is True
        ):
            # CSVs for add_media tasks don't contain 'title', so we need to get it.
            if config["task"] == "add_media":
                node_csv_row["title"] = get_node_title_from_nid(
                    config, node_csv_row["node_id"]
                )
                if node_csv_row["title"] is False:
                    message = (
                        "Cannot access node "
                        + str(node_id)
                        + ", so cannot get its title for use in media filename. Using filename instead."
                    )
                    logging.warning(message)
                    node_csv_row["title"] = os.path.basename(
                        node_csv_row[file_fieldname].strip()
                    )

            filename = re.sub("[^A-Za-z0-9]+", "_", node_csv_row["title"])
            filename = filename.strip("_")
            downloaded_file_path = os.path.join(
                subdir, filename + remote_extension_with_dot
            )
        elif (
            "use_nid_in_remote_filename" in config
            and config["use_nid_in_remote_filename"] is True
        ):
            filename = f"{node_id}{remote_extension_with_dot}"
            downloaded_file_path = os.path.join(subdir, filename)
        elif (
            config["field_for_remote_filename"] is not False
            and config["field_for_remote_filename"] in node_csv_row
            and len(node_csv_row[config["field_for_remote_filename"]]) > 0
        ):
            field_for_remote_filename_string = node_csv_row[
                config["field_for_remote_filename"]
            ][:255]
            sanitized_filename = re.sub(
                "[^0-9a-zA-Z]+", "_", field_for_remote_filename_string
            )
            downloaded_file_path = os.path.join(
                subdir, sanitized_filename.strip("_") + remote_extension_with_dot
            )
        else:
            # For files from Islandora Legacy ending in /view, we use the CSV ID as the filename.
            if len(remote_filename_parts[1]) == 0:
                filename = node_csv_row[config["id_field"]] + remote_extension_with_dot
            else:
                # For other files, we use the last part of the path preceding the file extension.
                url_path_parts = remote_filename_parts[0].split("/")
                filename = url_path_parts[-1] + remote_extension_with_dot
            downloaded_file_path = os.path.join(subdir, filename)

        # Check to see if a file with this path already exists; if so, insert an
        # incremented digit into the file path before the extension.
        if os.path.exists(downloaded_file_path):
            downloaded_file_path = get_deduped_file_path(downloaded_file_path)

        return downloaded_file_path
    # It's a local file.
    else:
        file_path_from_csv = os.path.expanduser(file_path_from_csv)
        if os.path.isabs(file_path_from_csv):
            file_path = file_path_from_csv
        else:
            file_path = os.path.join(config["input_dir"], file_path_from_csv)
        return file_path
