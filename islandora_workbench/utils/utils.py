import hashlib
import logging
import mimetypes
import os
import re
import subprocess
import sys
import zipfile
from typing import Union

import requests

from islandora_workbench.drupal.client import get_drupal_core_version
from islandora_workbench.exceptions import WorkbenchException
from islandora_workbench.utils.string_utils import convert_semver_to_number


def file_is_utf8(file_path: str) -> bool:
    """Check if a file is encoded as UTF-8, or backward-compatible encodings such as ASCII. BOM is ignored.
    Parameters
        ----------
        file_path : str
            The absolute or relative path to the file.
        Returns
        -------
        boolean
            True if file is encoded as UTF-8. False if not.
    :raise FileNotFoundError
        If the file is not found.
    """
    with open(file_path, "rb") as f:
        try:
            f.read().decode("utf-8-sig")
            return True
        except UnicodeDecodeError:
            return False

def execute_subprocess_script(config: dict, path_to_script: str,  **kwargs):
    """Generalized function to execute a subprocess script with the given configuration and parameters."""
    subdelimiter = config["subdelimiter"]
    config_file_path = config["config_file_path"]
    *interpreter, script = path_to_script.split(" ")
    cmd = subprocess.Popen(
        [*interpreter, script, subdelimiter, *list(kwargs)], stdout=subprocess.PIPE, )
    result, stderrdata = cmd.communicate()
    result = result.decode().strip()

    return result, cmd.returncode


def integration_supports_field_bundle(ctx: dict) -> bool:
    """True if use_workbench_permissions is on and the Drupal-side Integration
    module is new enough to provide the entity_field_bundle and media_actions endpoints.
    :param ctx: dict - The context dictionary containing the configuration settings.
    :return: bool - True if the Integration module supports field bundle, False otherwise.
    """
    version = ctx["INTEGRATION_MODULE_VERSION"]
    if not ctx["config"]["use_workbench_permissions"] or not version:
        return False
    return convert_semver_to_number(version) >= convert_semver_to_number("1.3.0")


def get_sequence_indicator_from_filename(config: dict, file_name: str) -> str:
    """Extracts the last segment of a page filename like some-ID-003.jpg.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        file_name : str
            The filename to extract the sequence indicator from.
        Returns
        -------
        str
    """
    filename_without_extension = os.path.splitext(file_name)[0]
    filename_segments = filename_without_extension.split(
        config["paged_content_sequence_separator"]
    )
    weight = filename_segments[-1]
    weight = weight.lstrip("0")
    return str(weight)


def get_mimetype_from_extension(
    config: dict, file_path: str, lazy: bool = False
) -> Union[str, None]:
    """For a given file path, return the corresponding MIME type.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
            The 'extensions_to_mimetypes' setting allows assignment of MIME types
            in config.
        file_path: string
            The path to the local file to get the MIME type for.
        lazy: bool
            If True, and no entry for a given extension exists in the map, return
            "application/octet-stream" as a default if non MIME type can be determined.
            If False, let Python's mimetypes library guess.
        Returns
        -------
        string|None
            The MIME type, or None if the MIME type can be determined.
    """
    if os.path.isabs(file_path) is True:
        filepath = file_path
    else:
        filepath = os.path.join(config["input_dir"], file_path)

    if os.path.exists(filepath):
        root, ext = os.path.splitext(filepath)
        ext = ext.lstrip(".").lower()
    else:
        logging.error(
            f"Attempt to get MIME type for file {filepath} failed because file does not exist."
        )
        return None

    # A MIME type used in Islandora but not recognized by Python's mimetypes library.
    mime_map = {
        "hocr": "text/vnd.hocr+html",
        "xml": "application/xml",
    }

    # Modify the map as per config.
    if (
        "extensions_to_mimetypes" in config
        and len(config["extensions_to_mimetypes"]) > 0
    ):
        for extension, mtype in config["extensions_to_mimetypes"].items():
            extension = extension.lstrip(".").lower()
            mime_map[extension] = mtype

    if ext in mime_map:
        return mime_map[ext]
    else:
        if lazy is False:
            return mimetypes.guess_type(filepath)[0]
        else:
            return "application/octet-stream"


def deduplicate_field_values(values: list) -> list:
    """Removes duplicate entries from 'values' while retaining
    the order of the unique members.
    Parameters
        ----------
        values : list
            List containing value(s) to dedupe. Members could be strings
            from CSV or dictionaries.
        Returns
        -------
        list
            A list of unique field values.
    """
    deduped_list = []
    for member in values:
        if member not in deduped_list:
            deduped_list.append(member)

    return deduped_list


def get_config_file_identifier(config: dict) -> str:
    """Gets a unique identifier of the current config file. Used in names of temp files, etc.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        Returns
        -------
        string
            A string based on the config file's path, with directory slashes and backslashes
            replaced with underscores.
    """
    split_path = os.path.splitdrive(
        os.path.splitext(config["current_config_file_path"])[0]
    )
    config_file_id = re.sub(r"[/\\]", "_", split_path[1].strip("/\\"))

    return config_file_id


def get_file_hash_from_local(file_path: str, algorithm: str) -> Union[bool, str]:
    """Get the file's hash/checksum.
    Parameters
        ----------
        file_path : string
            The file's absolute path.
        algorithm : string
            One of 'md5', 'sha1', or 'sha256'
        Returns
        -------
        string|bool
            The requested hash, False if the file doesn't exist.
    """
    if algorithm == "md5":
        hash_object = hashlib.md5()
    elif algorithm == "sha1":
        hash_object = hashlib.sha1()
    elif algorithm == "sha256":
        hash_object = hashlib.sha256()
    else:
        raise ValueError(f"Unsupported hash algorithm: {algorithm}")

    try:
        with open(file_path, "rb") as file:
            while True:
                chunk = file.read(hash_object.block_size)
                if not chunk:
                    break
                hash_object.update(chunk)

        return hash_object.hexdigest()
    except Exception as e:
        logging.error(f"Could not get hash for file {file_path}: {e}")
        return False


def check_for_required_config_keys(config: list, required_keys: tuple[str, ...], throw_exception: bool = False):
    """Check that the config file has all required keys.
    Parameters:
        config: (list) The configuration settings defined by workbench_config.get_config().
        required_keys: (tuple) A list of required keys that must be present in the config.
    Returns:
        None
    :raise KeyError: If any required keys are missing and throw_exception is True.
    """
    missing_keys = [k for k in required_keys if k not in config]
    if len(missing_keys) > 0:
        if throw_exception:
            raise KeyError(f"Missing required config keys: {', '.join(missing_keys)}")
        else:
            message = f"Please check your config file for required values: {', '.join(required_keys)}."
            logging.error(message)
            sys.exit("Error: " + message)


def prompt_user(ctx: dict):
    """Supply user prompts from the config file and exit if the user answer anything but Y or y.
    Parameters
    :param ctx: dict - The configuration settings and logging context.
    """
    for user_prompt in ctx["config"]["user_prompts"]:
        response = input(user_prompt)
        if response.lower() != "y":
            ctx["log"].info(
                f'Exiting because user responded "{response}" to prompt "{user_prompt}".'
            )
            sys.exit("Exiting at user prompts.")


def check_drupal_core_version(ctx: dict) -> None:
    """Used during --check to verify if the minimum required Drupal version for workbench is being used.

    Parameters
    ----------
    ctx : dict
        The context dictionary containing the configuration settings.
    Returns
    -------
    None
    """
    drupal_core_version = get_drupal_core_version(ctx)
    if drupal_core_version is not False:
        core_version_number = convert_semver_to_number(drupal_core_version)
    else:
        message = "Workbench cannot determine Drupal's version number."
        ctx["log"].error(message)
        raise WorkbenchException(f"Error: {message}")
    if core_version_number < tuple([8, 6]):
        message = (
            f"Media creation in your version of Drupal ({drupal_core_version}) is less reliable than in Drupal 8.6 or higher."
        )
        ctx["log"].warning(message)


def unzip_archive(config: dict, archive_file_path: Union[str, bool]) -> None:
    """Unzips a Zip archive to the input directory.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param archive_file_path: string|bool - The path to the Zip archive to unzip, or False if the download failed.
    """
    if archive_file_path is False:
        return None

    if os.path.exists(archive_file_path):
        if zipfile.is_zipfile(archive_file_path) is True:
            with zipfile.ZipFile(archive_file_path, "r") as zip_ref:
                zip_ref.extractall(config["input_dir"])
                message = f'Zip archive "{archive_file_path}" extracted to "{config["input_dir"]}".'
                print("OK, " + message)
                logging.info(message)

            if (
                archive_file_path.lower().startswith("http")
                and config["delete_zip_archive_after_extraction"] is True
            ):
                if config["check"] is False:
                    try:
                        os.remove(archive_file_path)
                        logging.info(f'Zip archive "{archive_file_path}" deleted."')
                    except Exception as e:
                        logging.error(
                            f'Could not remove input archive file "{archive_file_path}": {e}.'
                        )
        else:
            message = f'"{archive_file_path}" does not appear to be a valid Zip file.'
            logging.error(message)
            sys.exit("Error: " + message)
    else:
        message = f'Zip archive "{archive_file_path}" not extracted to "{config["input_dir"]}": cannot find zip archive.'
        logging.error(message)
        sys.exit("Error: " + message)


def check_file_exists(config: dict, filename: str) -> bool:
    """Confirms file exists and is a file (not a directory).
    For remote/downloaded files, checks for a 200 response from a HEAD request.

    Does not check whether filename value is blank.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        filename: string
            The filename or path.
        Returns
        -------
        boolean
            True if the file exists, false if not.
    """
    # It's a remote file.
    if filename.startswith("http"):
        try:
            headers = {"User-Agent": config["user_agent"]}
            cookies = {
                config["remote_file_cookie_name"]: config["remote_file_cookie_value"]
            }

            head_response = requests.head(
                filename,
                allow_redirects=True,
                verify=config["secure_ssl_only"],
                headers=headers,
                cookies=cookies,
            )
            if head_response.status_code == 200:
                return True
            else:
                return False
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as error:
            message = (
                "Workbench " + "timed out trying to reach "
                if isinstance(error, requests.exceptions.Timeout)
                else "cannot connect to " + filename + ". Details in next log entry."
            )
            logging.error(message)
            logging.error(error)
            return False

    # It's a local file.
    else:
        filename = os.path.expanduser(filename)
        if os.path.isabs(filename):
            file_path = filename
        else:
            file_path = os.path.join(config["input_dir"], filename)

        if os.path.isfile(file_path):
            return True
        else:
            return False


def get_extension_from_mimetype(config: dict, mimetype: str) -> Union[bool, str]:
    """For a given MIME type, return the corresponding file extension. mimetypes.add_type()
    is not working, e.g. mimetypes.add_type('image/jpeg', '.jpg'). Maybe related to
     https://bugs.python.org/issue4963? In the meantime, provide our own MIMETYPE to extension
     mapping for common types, then let mimetypes guess at others.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        mimetype: string
            The MIME type to get the corresponding extension for.
        Returns
        -------
        string|bool
            The extension, with a leading '.', or None if no extension can be determined.
    """
    map = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/jp2": ".jp2",
        "image/png": ".png",
        "image/tif": ".tif",
        "image/tiff": ".tif",
        "audio/mpeg": ".mp3",
        "text/plain": ".txt",
        "application/xml": ".xml",
        "application/octet-stream": ".bin",
    }
    if "mimetype_extensions" in config and len(config["mimetype_extensions"]) > 0:
        for mtype, ext in config["mimetype_extensions"].items():
            map[mtype] = ext

    if mimetype in map:
        return map[mimetype]
    else:
        return mimetypes.guess_extension(mimetype)
