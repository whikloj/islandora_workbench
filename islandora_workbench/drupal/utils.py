import json
import logging
import re
from typing import Union

import requests

from islandora_workbench.drupal.client import issue_request


def log_field_cardinality_violation(
    field_name: str, record_id: str, cardinality: str
) -> None:
    """Writes an entry to the log during create/update tasks if any field values
    are sliced off. Workbench does this if the number of values in a field
    exceeds the field's cardinality. record_id could be a value from the
    configured id_field or a node ID.
    :param field_name: str - The machine name of the field.
    :param record_id: str - The record identifier.
    :param cardinality: str - The field's cardinality.
    """
    logging.warning(
        "Adding all values in CSV field %s for record %s would exceed maximum number of allowed values (%s). Skipping adding extra values.",
        field_name,
        record_id,
        cardinality,
    )


def validate_language_code(langcode: str) -> bool:
    """Validates that 'langcode' is one of Drupal's supported language codes.
    :param langcode: str - The language code to validate.
    :return: bool - True if 'langcode' is valid, False if not.
    """
    # Drupal's language codes.
    codes = [
        "af",
        "am",
        "ar",
        "ast",
        "az",
        "be",
        "bg",
        "bn",
        "bo",
        "bs",
        "ca",
        "cs",
        "cy",
        "da",
        "de",
        "dz",
        "el",
        "en",
        "en-x-simple",
        "eo",
        "es",
        "et",
        "eu",
        "fa",
        "fi",
        "fil",
        "fo",
        "fr",
        "fy",
        "ga",
        "gd",
        "gl",
        "gsw-berne",
        "gu",
        "he",
        "hi",
        "hr",
        "ht",
        "hu",
        "hy",
        "id",
        "is",
        "it",
        "ja",
        "jv",
        "ka",
        "kk",
        "km",
        "kn",
        "ko",
        "ku",
        "ky",
        "lo",
        "lt",
        "lv",
        "mg",
        "mk",
        "ml",
        "mn",
        "mr",
        "ms",
        "my",
        "ne",
        "nl",
        "nb",
        "nn",
        "oc",
        "pa",
        "pl",
        "pt-pt",
        "pt-br",
        "ro",
        "ru",
        "sco",
        "se",
        "si",
        "sk",
        "sl",
        "sq",
        "sr",
        "sv",
        "sw",
        "ta",
        "ta-lk",
        "te",
        "th",
        "tr",
        "tyv",
        "ug",
        "uk",
        "ur",
        "vi",
        "xx-lolspeak",
        "zh-hans",
        "zh-hant",
    ]
    if langcode in codes:
        return True
    else:
        return False


def get_nid_from_url_alias(config: dict, url_alias_to_query: str) -> Union[int, bool]:
    """Gets a node ID from a URL alias. This function also works on canonical
    URLs, e.g. https://localhost:8000/node/1648 and URL aliases without a hostname,
    e.g., /i_am_an_alias.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    url_alias_to_query : string
        The full URL alias (or canonical URL), including https://, etc.
    Returns
    -------
    int|boolean
        The node ID, or False if the URL cannot be found.
    """
    if not url_alias_to_query:
        return False

    if url_alias_to_query.startswith("http"):
        # Drupal sometimes returns "http://" instead of "https://" in the "location"
        # response header. Check for that and replace it if necessary.
        if url_alias_to_query.startswith("http://") and config["host"].startswith(
            "https://"
        ):
            url_alias_to_query = re.sub(
                r"^http://", "https://", url_alias_to_query, flags=re.IGNORECASE
            )
        alias_query_url = f"{url_alias_to_query}?_format=json"
    else:
        alias_query_url = (
            f'{config["host"]}/{url_alias_to_query.lstrip("/")}?_format=json'
        )

    alias_query_response = issue_request(config, "GET", alias_query_url)
    if alias_query_response.status_code != 200:
        return False
    else:
        alias_query_node = alias_query_response.json()
        return alias_query_node["nid"][0]["value"]


def get_nid_from_url_without_config(url: str) -> Union[int, bool]:
    """Gets a node ID from a raw Drupal URL, with no accompanying config data. Useful
       within integration tests where the config is not directly accessible.

    Parameters
    ----------
    url : string
        The full URL alias (or canonical URL), including http://, etc.
    Returns
    -------
    int|boolean
        The node ID, or False if the URL cannot be found.
    """
    url = url + "?_format=json"
    response = requests.get(url, verify=False)
    if response.status_code != 200:
        return False
    else:
        media = json.loads(response.text)
        return media["nid"][0]["value"]


def get_node_title_from_nid(config: dict, node_id: str) -> Union[str, bool]:
    """Get node title from Drupal.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    node_id : string
        The node ID for the node title being fetched.
    Returns
    -------
    str|boolean
        The node title, or False if the URL does not return HTTP status 200.
    """
    node_url = config["host"] + "/node/" + node_id + "?_format=json"
    node_response = issue_request(config, "GET", node_url)
    if node_response.status_code == 200:
        node_dict = json.loads(node_response.text)
        return node_dict["title"][0]["value"]
    else:
        return False


def get_media_field_values(config: dict, media_id: Union[int, str]) -> dict:
    """Get a media's field data so we can use it during PATCH updates, which replace a field's values."""
    if config["standalone_media_url"] is True:
        media_url = config["host"] + "/media/" + media_id + "?_format=json"
    else:
        media_url = config["host"] + "/media/" + media_id + "/edit?_format=json"

    get_media_response = issue_request(config, "GET", media_url)
    media_fields = get_media_response.json()
    return media_fields
