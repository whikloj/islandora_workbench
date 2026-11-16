import json
import logging
import re
from typing import Union

import requests

from islandora_workbench.drupal.client import issue_request, get_term_name
from islandora_workbench.utils import value_is_numeric


def create_node(ctx: dict, node_data: dict) -> tuple[dict, dict]:
    """Creates a node in Drupal using the provided node data.
    :param ctx: dict - The configuration settings and logging context.
    :param node_data: dict - The node data to be sent in the request body.
    :return: tuple(dict, dict) - A tuple of the created node's data or empty dict and a dict of response, keys include status_code and location.
    """
    node_headers = {"Content-Type": "application/json"}
    node_endpoint = "/node?_format=json"
    node_response = issue_request(
        ctx["config"], "POST", node_endpoint, node_headers, node_data, None
    )
    response_dict = {
        "status_code": node_response.status_code,
        "location": node_response.headers.get("Location", None),
    }
    if node_response.status_code == 201:
        return node_response.json(), response_dict

    ctx["log"].error(
        "Problem creating node in Drupal. See log for details.",
        log_message=f"Problem in islandora_workbench.drupal.client::create_node: HTTP response code was {node_response.status_code}, response body was {node_response.content}"
    )
    ctx["log"].error(
        f"JSON request body used in previous POST to \"{node_endpoint}\" was {node_data}.",
        log_only=True
    )
    return {}, response_dict


def update_node(ctx: dict, node_id: Union[int, str], node_data: dict) -> dict:
    """Updates a node in Drupal using the provided node data.
    :param ctx: dict - The configuration settings and logging context.
    :param node_id: int|str - The ID of the node to update.
    :param node_data: dict - The node data to be sent in the request body.
    :return: dict - A dict of response, keys include status_code, response_body and location.
    """
    node_headers = {"Content-Type": "application/json"}
    node_endpoint = f"{ctx['config']['host']}/node/{node_id}?_format=json"
    node_response = issue_request(
        ctx["config"], "PATCH", node_endpoint, node_headers, node_data, None
    )
    response_dict = {
        "status_code": node_response.status_code,
        "location": node_response.headers.get("Location", None),
        "response_body": node_response.text,
    }
    if node_response.status_code == 200:
        return response_dict

    ctx["log"].error(
        "Problem updating node in Drupal. See log for details.",
        log_message=f"Problem in islandora_workbench.drupal.client::update_node: HTTP response code was {node_response.status_code}, response body was {node_response.content}"
    )
    ctx["log"].error(
        f"JSON request body used in previous PATCH to \"{node_endpoint}\" was {node_data}.",
        log_only=True
    )
    return response_dict


def delete_node(ctx: dict, node_id: Union[int, str]) -> dict:
    """Deletes a node in Drupal.
    :param ctx: dict - The configuration settings and logging context.
    :param node_id: int|str - The ID of the node to delete.
    :return: dict - A dict of response, keys include status_code and response_body.
    """
    node_endpoint = ctx["config"]["host"] + "/node/" + str(node_id) + "?_format=json"
    node_response = issue_request(ctx["config"], "DELETE", node_endpoint)
    return {
        "status_code": node_response.status_code,
        "response_body": node_response.text,
    }


def get_node_field_values(config: dict, nid: Union[int, str]) -> dict:
    """Get a node's field data so we can use it during PATCH updates, which replace a field's values."""
    if value_is_numeric(nid) is False:
        nid = get_nid_from_url_alias(config, nid)
    node_url = config["host"] + "/node/" + str(nid) + "?_format=json"
    response = issue_request(config, "GET", node_url)
    node_fields = response.json()
    return node_fields


def get_media_from_node(config: dict, nid: str) -> dict:
    """Get the media list for a node.
    :param config: dict - The configuration settings defined by workbench_config.get_config().
    :param nid: str - Node ID of the node being linked to by the media.
    :return: dict - The media list json for the node.
    """
    url = f"/node/{nid}/media?_format=json"
    try:
        response = issue_request(config, "GET", url)
        return response.json()
    except Exception as e:
        message = f"Getting media list for \"{config['host']}{url}\" returned an error."
        print(f"Error: {message} See log for more detail.")
        logging.error(f"{message} Detail: {e}")
        return {}

def get_node_media_summary(config: dict, nid: str) -> str:
    """Generates a formatted summary of what media a node has.

    Params
    ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        nid : string
            Node ID of the node being linked to by the media.
    Return
    ------
        str
            The summary.
    """
    media_list = get_media_from_node(config, nid)
    media_use_terms = [m["target_id"] for media in media_list if "field_media_use" in media for m in media["field_media_use"]]
    media_use_terms.sort()
    return "; ".join(media_use_terms).strip()


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
