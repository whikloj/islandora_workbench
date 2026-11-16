import hashlib
import logging
import mimetypes
import os
import re
import shutil
import string
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path
from typing import Union, OrderedDict, Any

import requests
import requests_cache

from islandora_workbench.drupal.client import get_drupal_core_version, get_term_uri, get_all_representations_of_term
from islandora_workbench.drupal.node import get_node_title_from_nid
from islandora_workbench.exceptions import WorkbenchException


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


def service_file_present(config: dict, input_str: str) -> bool:
    """Checks whether the input string contains the ServiceFile use term,
    or its ID or URI.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param input_str: string - The input string to check.
    :return: bool - True if the ServiceFile use term is present, False if not.
    """
    service_uri = "http://pcdm.org/use#ServiceFile"
    candidates = input_str.split("|")
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate == service_uri:
            return True
        if candidate.isnumeric():
            if get_term_uri(config, candidate) == service_uri:
                return True
        name_data = get_all_representations_of_term(
            config, vocab_id="islandora_media_use", name=candidate
        )
        if name_data["uri"] and name_data["uri"] == service_uri:
            return True
    return False


def clean_image_alt_text(input_string: str) -> str:
    """Strip out HTML markup to guard against CSRF in alt text."""
    cleaned_string = re.sub("<[^<]+?>", "", input_string)
    return cleaned_string


def get_deduped_file_path(path: str) -> str:
    """Given a file path, return a version of it that contains a version of
    the same name with an incremented integer inserted before the extension.
    Parameters
        ----------
        path : string
            The file path we want to dedupe.
        Returns
        -------
        string
            The deduped version of 'path', i.e., the original path with an
            underscore and an incremented digit inserted before the extension.
    """
    [base_path, extension] = os.path.splitext(path)

    numbers = re.findall(r"_\d+$", base_path)
    if len(numbers) == 0:
        incremented_path = base_path + "_1" + extension
    else:
        number = int(numbers[0].lstrip("_")) + 1
        base_path_parts = base_path.split("_")
        del base_path_parts[-1]
        incremented_path = "_".join(base_path_parts) + "_" + str(number) + extension

    return incremented_path


def convert_semver_to_number(version_string: str) -> tuple:
    """Convert a Semantic Version number (e.g. Drupal's) string to a tuple. We only need the
    major and minor numbers (e.g. 9.2).

    Parameters
    ----------
    version_string: string
        The version string as retrieved from Drupal.
    Returns
    -------
    tuple
        A tuple containing the major and minor Drupal core version numbers as integers.
    """
    parts = version_string.split(".")
    parts = parts[:2]
    int_parts = [int(part) for part in parts]
    version_tuple = tuple(int_parts)
    return version_tuple


def truncate_csv_value(field_name: str, record_id: str, field_config: dict, value: str):
    """Drupal will not accept text field values that have a length that
    exceeds the configured maximum length for that field. 'value'
    here is a field subvalue.
    """
    if isinstance(value, str) and "max_length" in field_config:
        max_length = field_config["max_length"]
        if max_length is not None and len(value) > int(max_length):
            original_value = value
            value = value[:max_length]
            logging.warning(
                'CSV field value "%s" in field "%s" (record ID %s) truncated at %s characters as required by the field\'s configuration.',
                original_value,
                field_name,
                record_id,
                max_length,
            )
    return value


def string_is_ascii(input_string: str) -> bool:
    """Check if a string contains only ASCII characters.
    Parameters
        ----------
        input_string : str
            The string to test.
        Returns
        -------
        boolean
            True if all characters are within the ASCII character set,
            False otherwise.
    """
    return all(ord(c) < 128 for c in input_string)


def split_media_track_string(config: dict, media_track_string: str) -> list:
    """Fields of type 'media_track' are represented in the CSV file using a structured string,
    specifically 'label:kind:srclang:path_to_vtt_file', e.g. "en:subtitles:en:path/to/the/vtt/file.vtt".
    This function takes one of those strings (optionally with a multivalue subdelimiter) and returns
    a list of dictionaries with 'label', 'kind', 'srclang', 'file_path' keys required by the
    'media_track' field type.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param media_track_string: str - The media track string from the CSV.
    :return: list - A list of dictionaries representing the media tracks with keys label, kind, srclang, and file_path.
    """
    media_track_string = media_track_string.strip()

    return_list = []
    if len(media_track_string) == 0:
        return return_list

    temp_list = media_track_string.split(config["subdelimiter"])
    for item in temp_list:
        track_parts_list = item.split(":", 3)
        item_dict = {
            "label": track_parts_list[0],
            "kind": track_parts_list[1],
            "srclang": track_parts_list[2],
            "file_path": track_parts_list[3],
        }
        return_list.append(item_dict)

    return return_list


def split_geolocation_string(config: dict, geolocation_string: str) -> list:
    """Fields of type 'geolocation' are represented in the CSV file using a
    structured string, specifically lat,lng, e.g. "49.16667, -123.93333"
    or "+49.16667, -123.93333". This function takes one of those strings
    (optionally with a multivalue subdelimiter) and returns a list of
    dictionaries with 'lat' and 'lng' keys required by the 'geolocation'
    field type.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param geolocation_string: str - The geolocation string from the CSV.
    :return: list - A list of dictionaries representing the geolocations with keys lat and lng.
    """
    geolocation_string = geolocation_string.strip()

    return_list = []
    if len(geolocation_string) == 0:
        return return_list

    temp_list = geolocation_string.split(config["subdelimiter"])
    for item in temp_list:
        item_list = item.split(",")
        # Remove any leading \ which might be in value if it comes from a spreadsheet.
        item_dict = {
            "lat": item_list[0].lstrip("\\").strip(),
            "lng": item_list[1].lstrip("\\").strip(),
        }
        return_list.append(item_dict)

    return return_list


def split_typed_relation_string(
    config: dict, typed_relation_string: str, target_type: str
) -> list:
    """Fields of type 'typed_relation' are represented in the CSV file
    using a structured string, specifically namespace:property:id,
    e.g., 'relators:pht:5'. 'id' is either a term ID or a node ID. This
    function takes one of those strings (optionally with a multivalue
    subdelimiter) and returns a list of dictionaries in the form they
    take in existing node values. ID values can also be term names (strings)
    and term URIs (also strings, but in the form 'http....').

    Also, these values can (but don't need to) have an optional namespace
    in the term ID segment, which is the vocabulary ID string. These
    typed relation strings look like 'relators:pht:person:Jordan, Mark'.
    However, since we split the typed relation strings only on the first
    two :, the entire third segment is considered, for the purposes of
    splitting the value, to be the term.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param typed_relation_string: str - The typed relation string from the CSV.
    :param target_type: str - The target type, currently only "taxonomy_term"
    :return: list - A list of dictionaries representing the typed relations with keys target_id, rel_type, and target_type.
    """
    typed_relation_string = typed_relation_string.strip()

    return_list = []
    if len(typed_relation_string) == 0:
        return return_list

    temp_list = typed_relation_string.split(config["subdelimiter"])
    for item in temp_list:
        item_list = item.split(":", 2)
        if value_is_numeric(item_list[2]):
            target_id = int(item_list[2])
        else:
            target_id = item_list[2]
        item_dict = {
            "target_id": target_id,
            "rel_type": item_list[0] + ":" + item_list[1],
            "target_type": target_type,
        }
        return_list.append(item_dict)

    return return_list


def compare_strings(known: str, unknown: str) -> bool:
    """Normalizes the unknown string and the known one, and compares
    them. If they match, returns True, if not, False. We could
    use FuzzyWuzzy or something but this is probably sufficient.
    Parameters
    :param known: string - The known string.
    :param unknown: string - The unknown string.
    :return: bool - True if they match, False if not.
    """
    # Strips leading and trailing whitespace.
    known = known.strip()
    unknown = unknown.strip()
    # Converts to lower case.
    known = known.lower()
    unknown = unknown.lower()
    # Remove all punctuation.
    for p in string.punctuation:
        known = known.replace(p, " ")
        unknown = unknown.replace(p, " ")
    # Replaces whitespace with a single space.
    known = " ".join(known.split())
    unknown = " ".join(unknown.split())

    if unknown == known:
        return True
    else:
        return False


def split_authority_link_string(config: dict, authority_link_string: str) -> list:
    """Fields of type 'authority_link' are represented in the CSV file using a structured string,
    specifically source%%uri%%title, e.g. "viaf%%http://viaf.org/viaf/153525475%%Rush (Musical group)".
    This function takes one of those strings (optionally with a multivalue subdelimiter)
    and returns a list of dictionaries with 'source', 'uri' and 'title' keys required by the
    'authority_link' field type.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param authority_link_string: str - The authority link string from the CSV.
    :return: list - A list of dictionaries representing the authority links with keys source, uri and title.
    """
    authority_link_string = authority_link_string.strip()

    return_list = []
    if len(authority_link_string) == 0:
        return return_list

    temp_list = authority_link_string.split(config["subdelimiter"])
    for item in temp_list:
        if item.count("%%") == 2:
            item_list = item.split("%%", 2)
            item_dict = {
                "source": item_list[0].strip(),
                "uri": item_list[1].strip(),
                "title": item_list[2].strip(),
            }
            return_list.append(item_dict)
        if item.count("%%") == 1:
            # There is no title.
            item_list = item.split("%%", 1)
            item_dict = {
                "source": item_list[0].strip(),
                "uri": item_list[1].strip(),
                "title": "",
            }
            return_list.append(item_dict)

    return return_list


def split_link_string(config: dict, link_string: str) -> list:
    """Fields of type 'link' are represented in the CSV file using a structured string,
    specifically uri%%title, e.g. "https://www.lib.sfu.ca%%SFU Library Website".
    This function takes one of those strings (optionally with a multivalue subdelimiter)
    and returns a list of dictionaries with 'uri' and 'title' keys required by the
    'link' field type.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param link_string: str - The link string from the CSV.
    :return: list - A list of dictionaries representing the links with keys uri and title.
    """
    link_string = link_string.strip()

    return_list = []
    if len(link_string) == 0:
        return return_list

    temp_list = link_string.split(config["subdelimiter"])
    for item in temp_list:
        if "%%" in item:
            item_list = item.split("%%", 1)
            item_dict = {"uri": item_list[0].strip(), "title": item_list[1].strip()}
            return_list.append(item_dict)
        else:
            # If there is no %% and title, use the URL as the title.
            item_dict = {"uri": item.strip(), "title": item.strip()}
            return_list.append(item_dict)

    return return_list


def value_is_numeric(value, allow_decimals: bool = False) -> bool:
    """Tests to see if value  is numeric.
    Parameters
    ----------
    value : varies
        The value to check. By design, we don't know what data type it is.
    allow_decimals: boolean
        Whether or not to allow '.' in the value. Decimal and float number types have decimals.

    Returns
    -------
    boolean
    """
    var = str(value)
    if allow_decimals is True and "." in str(value):
        var = str(value).replace(".", "")
    else:
        var = var.strip()
    if var.isnumeric():
        return True
    else:
        return False


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
    config: dict[str, Any],
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
    id_field = config["id_field"]
    if not id_field:
        if config["task"] == "add_media":
            id_field = "node_id"
        elif config["task"] in ["update_media", "update_media_by_node"]:
            id_field = "media_id"

    # Test whether file_path_from_csv is from one of the oEmbed providers
    # and if so, return it here.
    for provider_url, mtype in config["oembed_providers"].items():
        if file_path_from_csv.startswith(provider_url):
            return file_path_from_csv

    # It's a remote file.
    if file_path_from_csv.startswith("http"):
        if id_field not in node_csv_row:
            message = (
                f'Cannot determine subdirectory for file "{file_path_from_csv}" because CSV column "{id_field}" is missing.'
            )
            logging.error(message)
            raise SystemExit(message)
        subdir = os.path.join(
            config["temp_dir"], re.sub("[^A-Za-z0-9]+", "_", str(node_csv_row[id_field]) )
        )
        if make_dir:
            Path(subdir).mkdir(parents=True, exist_ok=True)

        if "check" in config.keys() and config["check"] is True:
            try:
                os.rmdir(subdir)
            except OSError as e:
                # This can happen if subdirectories from previous runs of Workbench exist.
                shutil.rmtree(subdir)
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
        return str(file_path)


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
