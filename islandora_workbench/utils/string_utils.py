import logging
import os
import re
import string

from islandora_workbench.drupal.client import get_term_uri, get_all_representations_of_term


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


def clean_image_alt_text(input_string: str) -> str:
    """Strip out HTML markup to guard against CSRF in alt text."""
    cleaned_string = re.sub("<[^<]+?>", "", input_string)
    return cleaned_string


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
