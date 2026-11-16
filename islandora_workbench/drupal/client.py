import copy
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.parse
from typing import Union, OrderedDict, Optional
from urllib.parse import urlparse

import requests
from cachetools import cached, LFUCache
from cachetools.keys import hashkey
from requests.adapters import HTTPAdapter
from requests.models import parse_url
from unidecode import unidecode
from urllib3 import Retry

from islandora_workbench.exceptions import WorkbenchException
from islandora_workbench.utils import integration_supports_field_bundle, clean_image_alt_text, get_deduped_file_path, value_is_numeric, download_remote_file, \
    check_file_exists

from workbench_utils import set_media_type, ping_remote_file
from islandora_workbench.drupal.media import resolve_media_use_term_id, get_media_list, find_file_url_in_media
from islandora_workbench.drupal.node import get_nid_from_url_alias

"""This file contains functions for sending or receiving data to/from Drupal and interpreting the responses."""


def issue_request(
    ctx: dict,
    method: str,
    path: str,
    headers: dict = None,
    json_data: dict = None,
    data: str = None,
    query: dict = None,
) -> requests.Response:
    """Issue the HTTP request to Drupal. Note: calls to non-Drupal URLs
    do not use this function.

    Parameters
    ----------
    ctx : dict
        The context dictionary containing configuration, logging and other relevant information.
    method : str
        The HTTP method to be issued for the request, e.g. POST or GET.
    path : str
        Path to the API endpoint that will be used for request.
    headers : dict, optional
        HTTP header information to be sent with request encoded as a dict.
    json_data : dict, optional
        Data to be sent with request body as JSON format, but encoded as a dict.
    data : str, optional
        Data to be sent in request body.
    query : dict, optional
        Request parameters sent as a dict.

    Returns
    -------
    requests.Response
    """
    with requests.Session() as session:
        retries = Retry(
            total=ctx["config"]["http_max_retries"],
            backoff_factor=ctx["config"]["http_backoff_factor"],
            status_forcelist=ctx["config"]["http_retry_on_status_codes"],
            allowed_methods=ctx["config"]["http_retry_allowed_methods"],
        )
        session.mount("http://", HTTPAdapter(max_retries=retries))
        session.mount("https://", HTTPAdapter(max_retries=retries))
        try:
            if ctx["config"]["secure_ssl_only"] is False:
                requests.packages.urllib3.disable_warnings()

            if not ctx["config"]["password"]:
                message = (
                    'Password for Drupal user not found. Please add the "password" option to your configuration '
                    + "file or provide the Drupal user's password in your ISLANDORA_WORKBENCH_PASSWORD environment variable."
                )
                ctx["log"].error(message)
                raise WorkbenchException(message)

            if ctx["config"]["check"] is False:
                if (
                    "pause" in ctx["config"]
                    # and method in ["POST", "PUT", "PATCH", "DELETE"]
                    and value_is_numeric(ctx["config"]["pause"])
                ):
                    time.sleep(int(ctx["config"]["pause"]))

            if headers is None:
                headers = dict()

            if query is None:
                query = dict()
            headers.update({"User-Agent": ctx["config"]["user_agent"]})

            # The trailing / is stripped in config, but we do it here too, just in case.
            ctx["config"]["host"] = ctx["config"]["host"].rstrip("/")
            if ctx["config"]["host"] in path:
                url = path
            else:
                # Since we remove the trailing / from the hostname, we need to ensure
                # that there is a / separating the host from the path.
                if not path.startswith("/"):
                    path = "/" + path
                url = ctx["config"]["host"] + path

            if ctx["config"]["log_request_url"] is True:
                ctx["log"].info(method + " " + url)

            if ctx["config"]["log_headers"] is True:
                ctx["log"].info(headers)
            if json_data is not None and ctx["config"]["log_json"] is True:
                try:
                    json_to_log = json.dumps(json_data)
                    ctx["log"].info(json_to_log, log_only=True)
                except Exception as e:
                    ctx["log"].warning(e)
                    ctx["log"].warning(
                        f"Can't convert data to JSON, logging it instead. More info may be available in the previous log message. Data is: {json_data}"
                    )
            response = session.request(
                method,
                url,
                allow_redirects=ctx["config"]["allow_redirects"],
                verify=ctx["config"]["secure_ssl_only"],
                auth=(ctx["config"]["username"], ctx["config"]["password"]),
                headers=headers,
                json=json_data,
                data=data,
                params=query,
                stream=True if method in ["PUT", "POST", "PATCH"] else False,
            )

            if ctx["config"]["log_response_status_code"] is True:
                ctx["log"].info(response.status_code)

            if ctx["config"]["log_response_body"] is True:
                ctx["log"].info(response.text)

            response_time = response.elapsed.total_seconds()
            average_response_time = ctx["response_time_tracker"].calculate_response_time_trend(response_time)

            log_response_time_value = copy.copy(ctx["config"]["log_response_time"])
            if "adaptive_pause" in ctx["config"] and value_is_numeric(
                ctx["config"]["adaptive_pause"]
            ):
                # Pause defined in config['adaptive_pause'] is included in the response time,
                # so we subtract it to get the "unpaused" response time.
                if average_response_time is not None and (
                    response_time - int(ctx["config"]["adaptive_pause"])
                ) > (average_response_time * int(ctx["config"]["adaptive_pause_threshold"])):
                    message = (
                        "HTTP requests paused for "
                        + str(ctx["config"]["adaptive_pause"])
                        + " seconds because request in next log entry "
                        + "exceeded adaptive threshold of "
                        + str(ctx["config"]["adaptive_pause_threshold"])
                        + "."
                    )
                    time.sleep(int(ctx["config"]["adaptive_pause"]))
                    ctx["log"].info(message)
                    # Enable response time logging if we surpass the adaptive pause threashold.
                    ctx["config"]["log_response_time"] = True

            if ctx["config"]["log_response_time"] is True:
                parsed_query_string = urlparse(url).query
                if len(parsed_query_string):
                    url_for_logging = (
                        urlparse(url).path + "?" + parsed_query_string
                    )
                else:
                    url_for_logging = urlparse(url).path
                if "adaptive_pause" in ctx["config"] and value_is_numeric(
                    ctx["config"]["adaptive_pause"]
                ):
                    response_time = response_time - int(ctx["config"]["adaptive_pause"])
                response_time_trend_entry = {
                    "method": method,
                    "response": response.status_code,
                    "url": url_for_logging,
                    "response_time": response_time,
                    "average_response_time": average_response_time,
                }
                ctx["log"].info(response_time_trend_entry)
                # Set this config option back to what it was before we updated in above.
                ctx["config"]["log_response_time"] = log_response_time_value
            return response
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.RequestException,
        ) as error:
            verb = (
                "timed out"
                if isinstance(error, requests.exceptions.Timeout)
                else (
                    f'could not connect to {ctx["config"]["host"]}'
                    if isinstance(error, requests.exceptions.ConnectionError)
                    else "encountered an exception"
                )
            )
            message = f'Workbench {verb} while requesting "{url}".'
            ctx["log"].error(message)
            ctx["log"].error(error)
            raise WorkbenchException(message)


def ping_islandora(ctx: dict, print_message: bool = True) -> bool:
    """Connect to Islandora in prep for subsequent HTTP requests.

    Parameters
    ----------
    ctx : dict
        The context object.
    print_message : boolean, optional
        If set to True, after ping successfully performed, a status message is printed for the user.
    Returns
    -------
    None
    """

    # First, test a known request that requires Administrator-level permissions.
    url = ctx["config"]["host"] + "/islandora_workbench_integration/version"
    try:
        host_response = issue_request(ctx["config"], "GET", url)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
        message = (
            "Workbench " + "timed out trying to reach "
            if isinstance(error, requests.exceptions.Timeout)
            else "cannot connect to "
            + ctx["config"]["host"]
            + '. Please verify the "host" setting in your configuration '
            + "and check your network connection."
        )
        ctx["log"].error(message)
        ctx["log"].error(error)
        raise WorkbenchException(error)

    not_authorized = [401, 403]
    if host_response.status_code in not_authorized:
        message = (
            "Workbench can connect to "
            + ctx["config"]["host"]
            + ' but the user "'
            + ctx["config"]["username"]
            + '" does not have sufficient permissions to continue, or the credentials are invalid.'
        )
        ctx["log"].error(message)
        raise WorkbenchException(message)

    if print_message:
        if ctx["config"]["secure_ssl_only"] is True:
            message = "OK, connection to Drupal at " + ctx["config"]["host"] + " verified."
        else:
            message = (
                "OK, connection to Drupal at "
                + ctx["config"]["host"]
                + " verified. Ignoring SSL certificates."
            )

        ctx["log"].info(message)


def verify_node_exists_by_key(config: dict, csv_row: dict) -> Union[str, bool]:
    """Query a View using a value from CSV (the "key") to see if the node exists.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    csv_row :
        A copy of the CSV row that represents the node we are interested in.

    Returns
    ------
     str|False
        The node ID if the node exists, False if the node doesn't exist, there are more than 1 node exists,
        or if there was a non-200 HTTP response.
    """
    endpoint_mapping = config["node_exists_verification_view_endpoint"]
    if len(csv_row[endpoint_mapping[0]]) == 0:
        row_id = csv_row[config["id_field"]]
        logging.warning(
            f'Can\'t verify node exists for item in row "{row_id}" since it has no value in its "{endpoint_mapping[0]}" CSV column.'
        )
        return False

    csv_value = copy.copy(csv_row[endpoint_mapping[0]])
    if config["subdelimiter"] in csv_value:
        csv_value_for_url = csv_value.replace(config["subdelimiter"], "%20")
    else:
        csv_value_for_url = csv_value

    view_url = f'{config["host"]}/{endpoint_mapping[1].lstrip("/")}?{endpoint_mapping[0]}={csv_value_for_url}'
    headers = {"Content-Type": "application/json"}
    response = issue_request(config, "GET", view_url, headers)
    if response.status_code == 200:
        body = response.json()
        if len(body) == 1:
            return body[0]["nid"]
        elif len(body) > 1:
            logging.warning(
                f'Query to View "{view_url}" found more than one node ({body}). CSV "{endpoint_mapping[0]}" value was {csv_row[endpoint_mapping[0]]}. Workbench skipped this CSV row.'
            )
        else:
            return False
    else:
        logging.warning(
            f"Query to View {view_url} encountered a problem: HTTP status code was {response.status_code}"
        )
        return False

def get_entity_fields(config: dict, entity_type: str, bundle_type: str) -> dict:
    """Get all the fields configured on a bundle.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().

    entity_type : string
        Values could be 'node', 'media', 'taxonomy_term', or 'paragraph'.
    bundle_type : string

    Returns
    -------
    dict
        If use_workbench_permissions is True and workbench version is >= 1.3.0, returns a dictionary with field names as keys, second level has keys 'config' and 'storage_config' with the Drupal data.
        Otherwise returns a dict with field names as keys and field names as values, e.g. {'field_name1': 'field_name1', 'field_name2': 'field_name2'}.

    """
    if ping_content_type(config) == 404:
        if config["use_workbench_permissions"] is True:
            # Could be because the Workbench Integration Module is not compatible with "use_workbench_permissions"
            # or because the specified content type doesn't exist.
            message = f"Workbench cannot determine if content type '{config['content_type']}' exists on {config['host']}."
        else:
            message = f"Content type '{config['content_type']}' does not exist on {config['host']}."
        logging.error(message)
        sys.exit("Error: " + message)
    fields_endpoint = (
        f"{config['host']}/islandora_workbench_integration/node_actions/entity_field_bundle/{entity_type}/{bundle_type}"
        if integration_supports_field_bundle(config)
        else (
            f"{config['host']}/islandora_workbench_integration/node_actions/entity_display/{entity_type}/{bundle_type}"
            if config["use_workbench_permissions"]
            else f"{config['host']}/entity/entity_form_display/{entity_type}.{bundle_type}.default?_format=json"
        )
    )
    bundle_type_response = issue_request(config, "GET", fields_endpoint)
    # If a vocabulary has no custom fields (like the default "Tags" vocab), this query will
    # return a 404 response. So, we need to use an alternative way to check if the vocabulary
    # really doesn't exist.
    if bundle_type_response.status_code == 404 and entity_type == "taxonomy_term":
        fallback_fields_endpoint = (
            "/entity/taxonomy_vocabulary/" + bundle_type + "?_format=json"
        )
        fallback_bundle_type_response = issue_request(
            config, "GET", fallback_fields_endpoint
        )
        # If this request confirms the vocabulary exists, its OK to make some assumptions
        # about what fields it has.
        if fallback_bundle_type_response.status_code == 200:
            return {}

    fields = {}
    if bundle_type_response.status_code == 200:
        node_config_raw = bundle_type_response.json()
        if integration_supports_field_bundle(config):
            fields = node_config_raw
        else:
            fieldname_prefix = "field.field." + entity_type + "." + bundle_type + "."
            fields = {
                field_dependency.replace(
                    fieldname_prefix, ""
                ): field_dependency.replace(fieldname_prefix, "")
                for field_dependency in node_config_raw["dependencies"]["config"]
                if field_dependency.startswith(fieldname_prefix)
            }
    else:
        message = "Workbench cannot retrieve field definitions from Drupal."
        message_detail = ""
        if config["task"] == "create_terms" or config["task"] == "update_terms":
            message_detail = f" Check that the vocabulary name identified in your vocab_id config setting is spelled correctly."
        elif config["task"] == "create" or config["task"] == "create_from_files":
            message_detail = f" Check that the content type named in your content_type config setting is spelled correctly."
        logging.error(
            message
            + message_detail
            + " HTTP response code was "
            + str(bundle_type_response.status_code)
            + "."
        )
        sys.exit("Error: " + message + " See the log for more information.")

    return fields


@cached(
    cache=LFUCache(128),
    key=lambda config, fieldname, entity_type, bundle_type: hashkey(
        fieldname, entity_type, bundle_type
    ),
)
def get_entity_field_config(
    config: dict, fieldname: str, entity_type: str, bundle_type: str
) -> str:
    """Get a specific field's configuration.

    Example query for taxonomy terms: /entity/field_config/taxonomy_term.islandora_media_use.field_external_uri?_format=json
    Parameters
    ----------
    :param config : dict
        The configuration settings defined by workbench_config.get_config().
    :param fieldname : string
        The machine name of the field whose configuration is being fetched.
    :param entity_type : string
        The Drupal entity type (i.e. 'node', 'media', or 'taxonomy_term').
    :param bundle_type : string
        The node content type, the vocabulary name, or the media type
    ----------
    :return: str
        The field configuration as a JSON string.
    """
    field_config_endpoint = (
        f"{config['host']}/islandora_workbench_integration/node_actions/field_config/{entity_type}/{bundle_type}/{fieldname}"
        if config["use_workbench_permissions"]
        else f"{config['host']}/entity/field_config/{entity_type}.{bundle_type}.{fieldname}?_format=json"
    )
    field_config_response = issue_request(config, "GET", field_config_endpoint)
    if field_config_response.status_code == 200:
        return field_config_response.text
    else:
        message = "Workbench cannot retrieve field definitions from Drupal. Please confirm that the Field, Field Storage, and Entity Form Display REST resources are enabled."
        logging.error(message)
        sys.exit("Error: " + message)


@cached(
    cache=LFUCache(128),
    key=lambda config, fieldname, entity_type: hashkey(fieldname, entity_type),
)
def get_entity_field_storage(config: dict, fieldname: str, entity_type: str) -> str:
    """Get a specific field's storage configuration.

    Example query for taxonomy terms: /entity/field_storage_config/taxonomy_term.field_external_uri?_format=json
    Parameters
    ----------
    :param config : dict
        The configuration settings defined by workbench_config.get_config().
    :param fieldname : string
        The machine name of the field whose configuration is being fetched.
    :param entity_type : string
        The Drupal entity type (i.e. 'node', 'media', or 'taxonomy_term').
    ----------
    :return: str
        The field configuration as a JSON string.
    """
    field_storage_endpoint = (
        f"{config['host']}/islandora_workbench_integration/node_actions/field_storage_config/{entity_type}/{fieldname}"
        if config["use_workbench_permissions"]
        else f"{config['host']}/entity/field_storage_config/{entity_type}.{fieldname}?_format=json"
    )
    field_storage_response = issue_request(config, "GET", field_storage_endpoint)
    if field_storage_response.status_code == 200:
        return field_storage_response.text
    else:
        message = "Workbench cannot retrieve field definitions from Drupal. Please confirm that the Field, Field Storage, and Entity Form Display REST resources are enabled."
        logging.error(message)
        sys.exit("Error: " + message)

@cached(
    cache=LFUCache(maxsize=20),
    key=lambda config, entity_type, bundle: f"{entity_type}:{bundle}",
)
def is_revisions_enabled(config: dict, entity_type: str, bundle: str):
    """Return True if the entity/bundle type has revisions enabled, False otherwise.

    Parameters:
        :param config: dict - The global configuration object.
        :param entity_type: str - The entity type (e.g., 'node', 'media').
        :param bundle: str - The entity's type (e.g., 'islandora_object', 'image').

    Returns:
        True if the type has revisions enabled, False otherwise.
    """
    if entity_type not in ["node", "media"]:
        logging.error(
            f"Invalid entity type {entity_type} passed to is_revisions_enabled function."
        )
        return False
    entity_type_str = "node_type" if entity_type == "node" else "media_type"
    media_type_endpoint = (
        f"{config['host']}/entity/{entity_type_str}/{bundle}?_format=json"
    )
    response = issue_request(config, "GET", media_type_endpoint)
    try:
        if response.status_code == 200:
            return response.json()["new_revision"]
        else:
            return False
    except requests.JSONDecodeError as e:
        logging.error(f"Error decoding JSON response from {media_type_endpoint}: {e}")
        return False


def get_integration_module_version(config: dict) -> Union[str, bool]:
    """Get the Islandora Workbench Integration module's version number.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    Returns
    -------
    string|False
        The version number string (i.e., may contain -dev, etc.) from the
        Islandora Workbench Integration module.
    """
    global workbench_integration_module_version
    if workbench_integration_module_version is None:
        url = config["host"] + "/islandora_workbench_integration/version"
        response = issue_request(config, "GET", url)
        if response.status_code == 200:
            version_body = response.json()
            workbench_integration_module_version = version_body[
                "integration_module_version"
            ]
        else:
            logging.warning(
                "Attempt to get the Islandora Workbench Integration module's version number returned a %s status code",
                response.status_code,
            )
            return False
    return workbench_integration_module_version


def ping_node(
    config: dict,
    nid_to_ping: str,
    method: str = "HEAD",
    return_json: bool = False,
    warn: bool = True,
) -> Union[bool, str]:
    """Ping the node to see if it exists.

    Note that HEAD requests do not return a response body.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    nid_to_ping : string
        Node ID/URL/alias of the node to be pinged.
    method: string, optional
        Either 'HEAD' or 'GET'.
    return_json: boolean, optional
        If True, return the entire response body to the caller.
    warn: boolean, optional
       If True, write a log entry.

    Returns
    ------
     boolean|str
        True if method is HEAD and node was found, the response JSON response
        body if method was GET. False if request returns a non-allowed status code.
    """
    incoming_nid_to_ping = copy.copy(nid_to_ping)
    if nid_to_ping is False:
        if warn is True:
            logging.warning(
                "Can't perform node ping because the provided node ID was 'False'."
            )
        return False

    if value_is_numeric(nid_to_ping) is False:
        nid_to_ping = get_nid_from_url_alias(config, nid_to_ping)
    url_to_ping = config["host"] + "/node/" + str(nid_to_ping) + "?_format=json"
    response = issue_request(config, method.upper(), url_to_ping)
    allowed_status_codes = [200, 301, 302]
    if response.status_code in allowed_status_codes:
        if return_json is True:
            return response.text
        else:
            return True
    else:
        if warn is True:
            logging.warning(
                "(%s) ping on node %s (using node ID %s) returned a %s status code.",
                method.upper(),
                url_to_ping,
                incoming_nid_to_ping,
                response.status_code,
            )
        return False


def ping_url_alias(config: dict, url_alias: str) -> int:
    """Ping the URL alias to see if it exists. Return the status code.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    url_alias : str
        The string with the URL alias being pinged.
    Returns
    -------
    int
        HTTP status code.
    """
    url = config["host"] + url_alias + "?_format=json"
    response = issue_request(config, "GET", url)
    return response.status_code


def ping_vocabulary(config: dict, vocab_id: str) -> bool:
    """Ping the node to see if it exists.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    vocab_id : str
        The string with the vocabulary ID being pinged.
    Returns
    -------
    boolean
        Returns Ture if HTTP status code returned is 200, if not False is returned.
    """
    url = (
        config["host"]
        + "/entity/taxonomy_vocabulary/"
        + vocab_id.strip()
        + "?_format=json"
    )
    response = issue_request(config, "GET", url)
    if response.status_code == 200:
        return True
    else:
        logging.warning(
            "Node ping (GET) on %s returned a %s status code.",
            url,
            response.status_code,
        )
        return False


def ping_term(config: dict, term_id: str) -> bool:
    """Ping the term to see if it exists.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    term_id : str
        The string with the term ID being pinged.
    Returns
    -------
    boolean
        Returns Ture if HTTP status code returned is 200, if not False is returned.
    """

    url = config["host"] + "/taxonomy/term/" + str(term_id).strip() + "?_format=json"
    response = issue_request(config, "GET", url)
    if response.status_code == 200:
        return True
    else:
        logging.warning(
            "Term ping (GET) on %s returned a %s status code.",
            url,
            response.status_code,
        )
        return False


def ping_content_type(config: dict) -> int:
    """Ping the content_type set in the configuration to see if it exists.
    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    Returns
    -------
    int
        The HTTP response code.
    """
    url = (
        f"{config['host']}/islandora_workbench_integration/node_actions/entity_field_bundle/node/{config['content_type']}"
        if integration_supports_field_bundle(config)
        else (
            f"{config['host']}/islandora_workbench_integration/node_actions/entity_display/node/{config['content_type']}"
            if config["use_workbench_permissions"]
            else f"{config['host']}/entity/entity_form_display/node.{config['content_type']}.default?_format=json"
        )
    )

    return issue_request(config, "GET", url).status_code


def ping_view_endpoint(config: dict, view_url: str) -> int:
    """Verifies that the View REST endpoint is accessible.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    view_url
        The View's REST export path.
    Returns
    -------
    int
        The HTTP response code.

    Note: This checks specifically for 401 (the confirmed failure code) rather
    than any non-200 result, as a deliberately conservative first step — if another
    failure mode surfaces later (e.g. a different status code from a different Drupal
    configuration), this condition may need broadening to catch it too.
    """
    status_code = issue_request(config, "HEAD", view_url).status_code
    if status_code == 401:
        status_code = issue_request(config, "GET", view_url).status_code
    return status_code


def ping_entity_reference_view_endpoint(
    config: dict, fieldname: str, handler_settings: dict
) -> bool:
    """Verifies that the REST endpoint of the View is accessible. The path to this endpoint
    is defined in the configuration file's 'entity_reference_view_endpoints' option.

    Necessary for entity reference fields configured as "Views: Filter by an entity reference View".
    Unlike Views endpoints for taxonomy entity reference fields configured using the "default"
    entity reference method, the Islandora Workbench Integration module does not provide a generic
    Views REST endpoint that can be used to validate values in this type of field.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    fieldname : string
        The name of the Drupal field.
    handler_settings : dict (NOT USED)
        The handler_settings values from the field's configuration.
        # handler_settings': {'view': {'view_name': 'mj_entity_reference_test', 'display_name': 'entity_reference_1', 'arguments': []}}
    Returns
    -------
    bool
        True if the REST endpoint is accessible, False if not.
    """
    if "entity_reference_view_endpoints" not in config:
        logging.warning(
            "The 'entity_reference_view_endpoints' option in your configuration file does not contain any field-Views endpoint mappings."
        )
        return False
    else:
        endpoint_mappings = config["entity_reference_view_endpoints"]
        if fieldname not in endpoint_mappings:
            logging.warning(
                'The field "'
                + fieldname
                + '" is not in your "entity_reference_view_endpoints" configuration option.'
            )
            return False

        # E.g., "http://localhost:8000/issue_452_test?name=xxx&_format=json"
        url = config["host"] + endpoint_mappings[fieldname] + "?name=xxx&_format=json"
        response = issue_request(config, "GET", url)
        if response.status_code == 200:
            return True
        else:
            logging.warning(
                "View REST export ping (GET) on %s returned a %s status code",
                url,
                response.status_code,
            )
            return False


def ping_media_bundle(config: dict, bundle_name: str) -> int:
    """Ping the Media bundle/type to see if it exists. Return the status code,
    a 200 if it exists or a 404 if it doesn't exist or the Media Type REST resource
    is not enabled on the target Drupal.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    bundle_name : str
        Media bundle/type to be pinged.
    Returns
    -------
    int
        The HTTP response code.
    """
    url = (
        config["host"]
        + f"/islandora_workbench_integration/media_actions/entity_type/{bundle_name}"
        if integration_supports_field_bundle(config)
        else config["host"] + "/entity/media_type/" + bundle_name + "?_format=json"
    )
    response = issue_request(config, "GET", url)
    return response.status_code


def ping_media(
    config: dict,
    mid: Union[int, str],
    method: str = "HEAD",
    return_json: bool = False,
    warn: bool = True,
) -> Union[bool, dict]:
    """Ping the media to see if it exists.

    Note that HEAD requests do not return a response body.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    mid :
        Media ID of the media to be pinged. Could be a numeric media ID or a full URL to the media.
    method: string, optional
        Either 'HEAD' or 'GET'.
    return_json: boolean, optional
    warn: boolean, optional

    Returns
    ------
     boolean|dict
        True if method is HEAD and node was found, the response JSON response
        body if method was GET. False if request returns a non-allowed status code.
    """
    if value_is_numeric(mid) is False:
        mid = get_mid_from_media_url_alias(config, mid)

    if config["standalone_media_url"] is True:
        url = config["host"] + "/media/" + str(mid) + "?_format=json"
    else:
        url = config["host"] + "/media/" + str(mid) + "/edit?_format=json"

    response = issue_request(config, method.upper(), url)
    allowed_status_codes = [200, 301, 302]
    if response.status_code in allowed_status_codes:
        if return_json is True:
            return response.json()
        else:
            return True
    else:
        if warn is True:
            logging.warning(
                "Media ping (%s) on %s returned a %s status code.",
                method.upper(),
                url,
                response.status_code,
            )
        return False


def get_term_name(config: dict, term_id: Union[int, str]) -> Union[str, bool]:
    """Get the term's name and return it. If the term doesn't exist, return False.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param term_id: int|string - The term ID.
    :return: str|bool - The term name, or False if the term doesn't exist.
    """
    url = config["host"] + "/taxonomy/term/" + str(term_id).strip() + "?_format=json"
    response = issue_request(config, "GET", url)
    if response.status_code == 200:
        term_data = response.json()
        return term_data["name"][0]["value"]
    else:
        logging.warning(
            'Query for term ID "%s" returned a %s status code',
            term_id,
            response.status_code,
        )
        return False


def get_term_uri(config: dict, term_id: Union[int, str]) -> Union[str, None, bool]:
    """Get the term's URI and return it. If the term or URI doesn't exist, return False.
    If the term has no URI, return None.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param term_id: int|string - The term ID.
    :return: str|bool|None - The term URI, or None if there isn't one or False if the term doesn't exist.
    """
    url = config["host"] + "/taxonomy/term/" + str(term_id).strip() + "?_format=json"
    response = issue_request(config, "GET", url)
    if response.status_code == 200:
        term_data = response.json()
        if "field_external_uri" in term_data:
            uri = term_data["field_external_uri"][0]["uri"]
            return uri
        elif "field_authority_link" in term_data:
            uri = term_data["field_authority_link"][0]["uri"]
            return uri
        else:
            logging.warning(
                'Query for term ID "%s" does not have either a field_authority_link or field_exteral_uri field.',
                term_id,
            )
            return None
    else:
        logging.warning(
            'Query for term ID "%s" returned a %s status code',
            term_id,
            response.status_code,
        )
        return False


def get_term_id_from_uri(config: dict, uri: str) -> Union[int, bool]:
    """For a given URI, query the Term from URI View created by the Islandora
    Workbench Integration module. Because we don't know which field each
    taxonomy uses to store URIs (it's either field_external_uri or field_authority_link),
    we need to check both options in the "Term from URI" View.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param uri: string - The term URI.
    :return: int|bool - The term ID, or False if the term doesn't exist.
    """
    # Some vocabularies use this View.
    terms_with_uri = []
    term_from_uri_url = (
        config["host"] + "/term_from_uri?_format=json&uri=" + uri.replace("#", "%23")
    )
    term_from_uri_response = issue_request(config, "GET", term_from_uri_url)
    if term_from_uri_response.status_code == 200:
        term_from_uri_response_body = term_from_uri_response.json()
        if len(term_from_uri_response_body) == 1:
            tid = term_from_uri_response_body[0]["tid"][0]["value"]
            return tid
        elif len(term_from_uri_response_body) > 1:
            for term in term_from_uri_response_body:
                terms_with_uri.append(
                    {term["tid"][0]["value"]: term["vid"][0]["target_id"]}
                )
                tid = term_from_uri_response_body[0]["tid"][0]["value"]
            print("Warning: See log for important message about use of term URIs.")
            logging.warning(
                'Term URI "%s" is used for more than one term (with these term ID/vocabulary ID combinations: '
                + str(terms_with_uri)
                + "). Workbench is choosing the first term ID (%s)).",
                uri,
                tid,
            )
            return tid

    # And some vocabuluaries use this View.
    term_from_authority_link_url = (
        config["host"]
        + "/term_from_authority_link?_format=json&authority_link="
        + uri.replace("#", "%23")
    )
    term_from_authority_link_response = issue_request(
        config, "GET", term_from_authority_link_url
    )
    if term_from_authority_link_response.status_code == 200:
        term_from_authority_link_response_body = (
            term_from_authority_link_response.json()
        )
        if len(term_from_authority_link_response_body) == 1:
            tid = term_from_authority_link_response_body[0]["tid"][0]["value"]
            return tid
        elif len(term_from_authority_link_response_body) > 1:
            for term in term_from_authority_link_response_body:
                terms_with_uri.append(
                    {term["tid"][0]["value"]: term["vid"][0]["target_id"]}
                )
                tid = term_from_authority_link_response_body[0]["tid"][0]["value"]
            print("Warning: See log for important message about use of term URIs.")
            logging.warning(
                'Term URI "%s" is used for more than one term (with these term ID/vocabulary ID combinations: '
                + str(terms_with_uri)
                + "). Workbench is choosing the first term ID (%s)).",
                uri,
                tid,
            )
            return tid
        else:
            # URI does not match any term.
            return False

    # Non-200 response code.
    return False

@cached(
    cache=LFUCache(maxsize=20),
    key=lambda config, term_id: hashkey(config["host"], term_id),
)
def get_term_vocab(config: dict, term_id: Union[int, str]) -> Union[str, bool]:
    """Get the term's parent vocabulary ID and return it. If the term doesn't
    exist, return False.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param term_id: int|string - The term ID.
    :return: str|bool - The vocabulary ID, or False if the term doesn't exist.
    """
    url = config["host"] + "/taxonomy/term/" + str(term_id).strip() + "?_format=json"
    response = issue_request(config, "GET", url)
    if response.status_code == 200:
        term_data = response.json()
        return term_data["vid"][0]["target_id"]
    else:
        logging.warning(
            'Query for term ID "%s" returned a %s status code',
            term_id,
            response.status_code,
        )
        return False

@cached(
    cache=LFUCache(maxsize=20),
    key=lambda config, vocab_id, term_name_to_find: hashkey(config["host"], vocab_id, term_name_to_find, "check" in config.keys() and config["check"] is True),
)
def find_term_in_vocab(
    config: dict, vocab_id: str, term_name_to_find: str
) -> Union[int, bool]:
    """Query the Term from term name View using the vocab_id to see if term_name_to_find is
    is found in that vocabulary. If so, returns the term ID; if not returns False. If
    more than one term found, returns the term ID of the first one. Also populates global
    lists of terms (checked_terms and newly_created_terms) to reduce queries to Drupal.
    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    vocab_id: string
        The vocabulary ID to use in the query to find the term.
    term_name_to_find: string
        The term name from CSV.
    Returns
    -------
    int|boolean
        The term ID, existing or newly created. Returns False if term name
        is not found (or config['validate_terms_exist'] is False).
    """
    if "check" in config.keys() and config["check"] is True:
        # todo: move this check outside to the calling function.
        if config["validate_terms_exist"] is False:
            return False

        # Attempt to detect term names (including typed relation taxonomy terms) that are namespaced. Some term names may
        # contain a colon (which is used in the incoming CSV to seprate the vocab ID from the term name). If there is
        # a ':', maybe it's part of the term name and it's not namespaced. To find out, split term_name_to_find
        # and compare the first segment with the vocab_id.
        if ":" in term_name_to_find:
            original_term_name_to_find = copy.copy(term_name_to_find)
            [tentative_vocab_id, tentative_term_name] = term_name_to_find.split(
                ":", maxsplit=1
            )
            if tentative_vocab_id.strip() == vocab_id.strip():
                term_name_to_find = tentative_term_name
            else:
                term_name_to_find = original_term_name_to_find

        """
        # Namespaced terms (inc. typed relation terms): if a vocabulary namespace is present, we need to split it out
        # from the term name. This only applies in --check since namespaced terms are parsed in prepare_term_id().
        # Assumptions: the term namespace always directly precedes the term name, and the term name may
        # contain a colon. See https://github.com/mjordan/islandora_workbench/issues/361 for related logic.
        namespaced = re.search(':', term_name_to_find)
        if namespaced:
            namespaced_term_parts = term_name_to_find.split(':')
            # Assumption is that the term name is the last part, and the namespace is the second-last.
            term_name_to_find = namespaced_term_parts[-1]
            vocab_id = namespaced_term_parts[-2]
        """

        # Skipping this and caching the entire function instead.
        #term_name_for_check_matching = term_name_to_find.lower().strip()
        #for checked_term in checked_terms:
        #    if (
        #        checked_term["vocab_id"] == vocab_id
        #        and checked_term["name_for_matching"] == term_name_for_check_matching
        #    ):
        #        if value_is_numeric(checked_term["tid"]):
        #            return checked_term["tid"]
        #        else:
        #            return False

    # Skipping this and caching the entire function instead.
    #for newly_created_term in newly_created_terms:
    #    if (
    #        newly_created_term["vocab_id"] == vocab_id
    #        and newly_created_term["name_for_matching"]
    #        == term_name_to_find.lower().strip()
    #    ):
    #        return newly_created_term["tid"]

    url = (
        config["host"]
        + "/term_from_term_name?vocab="
        + vocab_id.strip()
        + "&name="
        + urllib.parse.quote_plus(term_name_to_find.strip())
        + "&_format=json"
    )
    response = issue_request(config, "GET", url)
    if response.status_code == 200:
        term_data = response.json()
        # Term name is not found.
        if len(term_data) == 0:
            return False
        elif len(term_data) > 1:
            print(
                "Warning: See log for important message about duplicate terms within the same vocabulary."
            )
            logging.warning(
                'Query for term "%s" found %s terms with that name in the %s vocabulary. Workbench is choosing the first term ID (%s)).',
                term_name_to_find,
                len(term_data),
                vocab_id,
                term_data[0]["tid"][0]["value"],
            )
            return term_data[0]["tid"][0]["value"]
        # Term name is found.
        else:
            return term_data[0]["tid"][0]["value"]
    else:
        logging.warning(
            'Query for term "%s" in vocabulary "%s" returned a %s status code',
            term_name_to_find,
            vocab_id,
            response.status_code,
        )
        return False


def get_all_representations_of_term(
    config: dict,
    vocab_id: str = None,
    name: str = None,
    term_id: int = None,
    uri: str = None,
) -> Union[dict, bool]:
    """Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    vocab_id: string
        The vocabulary ID to use in the query to find the term. Required if 'name' is the other arguments.
    name: string
        The term name.
    term_id: int
        The term ID. Does not require any other named arguments, since term IDs are unique.
    uri: string
        The term URI. Does not require any other named arguments, since term IDs (in theory) are unique.
    Returns
    -------
    dict/False
        A dictionary containing keys 'term_id', 'name', and 'uri'. False if there is insufficient
        information to get all representations of the term.
    """
    if term_id is not None and value_is_numeric(term_id):
        name = get_term_name(config, term_id)
        uri = get_term_uri(config, term_id)
    elif name is not None:
        if vocab_id is None:
            return False
        term_id = find_term_in_vocab(config, vocab_id, name)
        uri = get_term_uri(config, term_id)
    elif uri is not None:
        term_id = get_term_id_from_uri(config, uri)
        name = get_term_name(config, term_id)

    return {"term_id": term_id, "name": name, "uri": uri}


def send_to_term_endpoint(config: dict, term: dict):
    """Sends a term to the Drupal taxonomy/term endpoint.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param term: dict - The term to create.
    :return: int|False - The term ID if the term was created, False otherwise.
    """
    term_endpoint = config["host"] + "/taxonomy/term?_format=json"
    headers = {"Content-Type": "application/json"}
    response = issue_request(config, "POST", term_endpoint, headers, term, None)
    if response.status_code == 201:
        term_response_body = response.json()
        tid = term_response_body["tid"][0]["value"]
        if (config["task"] == "create" or config["task"] == "update") and config[
            "log_term_creation"
        ] is True:
            logging.info(
                'Term %s ("%s") added to vocabulary "%s".', tid, term["name"][0]["value"], term["vid"][0]["target_id"]
            )
        if config["task"] == "create_terms":
            logging.info(
                'Term %s ("%s") added to vocabulary "%s".', tid, term["name"][0]["value"], term["vid"][0]["target_id"]
            )
        return tid
    else:
        logging.warning(
            "Term '%s' not created, HTTP response code was %s, response body was %s.",
            term["name"][0]["value"],
            response.status_code,
            response.text,
        )
        logging.error(
            'JSON request body used in previous POST to "%s" was %s.',
            term_endpoint,
            term,
        )

        return False


def get_term_uuid(config: dict, term_id: Union[int, str]) -> str:
    """Given a term ID, get the term's UUID.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param term_id: int|string - The term ID.
    :return: str - The term UUID.
    """
    term_url = config["host"] + "/taxonomy/term/" + str(term_id) + "?_format=json"
    response = issue_request(config, "GET", term_url)
    term = response.json()
    uuid = term["uuid"][0]["value"]

    return uuid


def create_url_alias(config: dict, node_id: Union[int, str], url_alias: str) -> None:
    """Creates a URL alias for the given node ID.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param node_id: int|string - The node ID.
    :param url_alias: string - The desired URL alias.
    """
    json = {
        "path": [{"value": "/node/" + str(node_id)}],
        "alias": [{"value": url_alias}],
    }

    headers = {"Content-Type": "application/json"}
    response = issue_request(
        config,
        "POST",
        config["host"] + "/entity/path_alias?_format=json",
        headers,
        json,
        None,
    )
    if response.status_code != 201:
        logging.error(
            "URL alias '%s' not created for node %s, HTTP response code was %s (it might already exist).",
            url_alias,
            config["host"] + "/node/" + str(node_id),
            response.status_code,
        )


def get_mid_from_media_url_alias(config: dict, url_alias: str) -> Union[int, bool]:
    """Gets a media ID from a media URL alias. This function also works
    with canonical URLs, e.g. http://localhost:8000/media/1234.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    url_alias : string
        The full URL alias (or canonical URL), including http://, etc.
    Returns
    -------
    int|boolean
        The media ID, or False if the URL cannot be found.
    """
    # Drupal sometimes returns "http://" instead of "https://" in the "location"
    # response header. Check for that and replace it if necessary.
    parsed = parse_url(url_alias)
    if parsed.scheme == "http" and config["host"].startswith("https://"):
        url_alias = re.sub(r"^http://", "https://", url_alias, flags=re.IGNORECASE)
        url_alias = f"{config['host']}{parsed.path}"


    url = url_alias + "?_format=json"
    response = issue_request(config, "GET", url)
    if response.status_code != 200:
        return False
    else:
        media = json.loads(response.text)
        return media["mid"][0]["value"]


def get_tid_from_term_url_alias(config: dict, url_alias: str) -> Union[int, bool]:
    """Gets a term ID from a term URL alias. This function also works
    with canonical URLs, e.g. http://localhost:8000/taxonomy/term/1234.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    url_alias : string
        The full URL alias (or canonical URL), including http://, etc.
    Returns
    -------
    int|boolean
        The term ID, or False if the URL cannot be found.
    """
    # Drupal sometimes returns "http://" instead of "https://" in the "location"
    # response header. Check for that and replace it if necessary.
    if url_alias.startswith("http://") and config["host"].startswith("https://"):
        url_alias = re.sub(r"^http://", "https://", url_alias, flags=re.IGNORECASE)

    url = url_alias + "?_format=json"
    response = issue_request(config, "GET", url)
    if response.status_code != 200:
        return False
    else:
        term = json.loads(response.text)
        return term["tid"][0]["value"]

def ping_uid(config: dict, uid: Union[int, str]) -> bool:
    """Ping the user to see if it exists.

    Parameters
    ----------
    config : dict
        The configuration settings.
    uid : int|str
        User ID of the user to be pinged. Could be a numeric user ID or a full URL to the user.

    Returns
    -------
    boolean
        True if the user exists, False if not.
    """
    url = config["host"] + "/user/" + str(uid) + "?_format=json"
    response = issue_request(config, "GET", url)
    if response.status_code == 200:
        return True
    else:
        return False


def get_file_hash_from_drupal(
    config: dict, file_uuid: str, algorithm: str
) -> Union[bool, str]:
    """Query the Integration module's hash controller at '/islandora_workbench_integration/file_hash'
    to get the hash of the file identified by file_uuid.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        file_uuid : string
            The file's UUID.
        algorithm : string
            One of 'md5', 'sha1', or 'sha256'
        Returns
        -------
        string|bool
            The requested hash or False on failure.
    """
    url = (
        config["host"]
        + "/islandora_workbench_integration/file_hash?file_uuid="
        + file_uuid
        + "&algorithm="
        + algorithm
    )
    response = issue_request(config, "GET", url)
    if response.status_code == 200:
        try:
            response_body = response.json()
            return response_body[0]["checksum"]
        except requests.exceptions.JSONDecodeError as e:
            logging.warning("Unable to decode JSON response: %s", e)
    else:
        logging.warning(
            "Request to get %s hash for file %s returned a %s status code",
            algorithm,
            file_uuid,
            response.status_code,
        )
        return False


def get_drupal_core_version(ctx: dict) -> Union[str, bool]:
    """Get Drupal's version number.

    Parameters
    ----------
    ctx : dict
        The context dictionary containing the configuration settings.
    Returns
    -------
    string|False
        The Drupal core version number string (i.e., may contain -dev, etc.).
    """
    url = ctx["config"]["host"] + "/islandora_workbench_integration/core_version"
    response = issue_request(ctx["config"], "GET", url)
    if response.status_code == 200:
        version_body = response.json()
        return version_body["core_version"]
    else:
        ctx["log"].warning(
            f"Attempt to get Drupal core version number returned a {response.status_code} status code"
        )
        return False


def patch_image_alt_text(
    config: dict, media_id: Union[int, str], csv_row: OrderedDict
) -> Union[int, bool]:
    """Patch the alt text value for an image media. Use the parent node's title
    unless the CSV record contains an image_alt_text field with something in it.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param media_id: int|str - The media ID to patch.
    :param csv_row: OrderedDict - The CSV row of the parent node.
    :return: int|bool - The HTTP status code from the PATCH request, or False on failure.
    """
    if config["standalone_media_url"] is True:
        media_endpoint = f"{config['host']}/media/{media_id}?_format=json"
    else:
        media_endpoint = f"{config['host']}/media/{media_id}/edit?_format=json"

    get_media_headers = {"Content-Type": "application/json"}
    get_media_response = issue_request(
        config, "GET", media_endpoint, get_media_headers
    )
    if get_media_response.status_code == 200:
        get_media_response_body = json.loads(get_media_response.text)
        field_media_image_target_id = get_media_response_body["field_media_image"][0][
            "target_id"
        ]
    else:
        logging.error(
            f"Media {media_endpoint} returned an HTTP status code of {get_media_response.status_code}."
        )
        return False

    alt_text = ""
    for field_name, field_value in csv_row.items():
        if field_name in ["title", "image_alt_text"]:
            alt_text = clean_image_alt_text(field_value)
        # "image_alt_text" can be in "create", "add_alt_text", or "update_alt_text" input CSV.

    max_image_alt_text_length = config["max_image_alt_text_length"]
    # TODO: alt_text may be undefined here if neither 'title' nor 'image_alt_text' is in csv_row.
    if len(alt_text) > max_image_alt_text_length:
        logging.warning(
            f'Alt text "{alt_text}" is longer than the configured maximum length ({max_image_alt_text_length}), skipping adding it to image.'
        )
        return False

    media_json = {
        "bundle": [{"target_id": "image"}],
        "field_media_image": [
            {"target_id": field_media_image_target_id, "alt": alt_text}
        ],
    }

    patch_media_headers = {"Content-Type": "application/json"}
    patch_media_response = issue_request(
        config, "PATCH", media_endpoint, patch_media_headers, media_json
    )

    if patch_media_response.status_code != 200:
        logging.error("Alt text for image media %s not updated.", media_endpoint)

    return patch_media_response.status_code


def create_file(
    config: dict,
    filename: str,
    file_fieldname: str,
    node_csv_row: OrderedDict,
    node_id: str,
) -> Union[int, bool, None]:
    """Creates a file in Drupal, which is then referenced by the accompanying media.
    Parameters
    ----------
     config : dict
         The configuration settings defined by workbench_config.get_config().
     filename : string
         The full path to the file (either from the 'file' CSV column or downloaded from somewhere).
     file_fieldname: string
         The name of the CSV column containing the filename. None if the file isn't
         in a CSV field (e.g., when config['paged_content_from_directories'] is True or
         config["paged_content_from_directories_parents_exist"] is True).
     node_csv_row: OrderedDict
         E.g., OrderedDict([('file', 'IMG_5083.JPG'), ('id', '05'), ('title', 'Alcatraz Island').
     node_id: string
         The nid of the parent media's parent node.
     Returns
     -------
     int|bool|None
         The file ID (int) of the successfully created file; False if there is insufficient
         information to create the file or file creation failed, or None if config['nodes_only'].
    """
    if config["nodes_only"] is True:
        return None

    if config["task"] == "add_media" or config["task"] == "create":
        if (
            file_fieldname is not None
            and len(node_csv_row[file_fieldname].strip()) == 0
        ):
            return None

    is_remote = False
    filename = filename.strip()

    if filename.startswith("http"):
        remote_file_http_response_code = ping_remote_file(config, filename)
        if remote_file_http_response_code != 200:
            return False

        file_path = download_remote_file(
            config, filename, file_fieldname, node_csv_row, node_id
        )
        if file_path is False:
            return False
        filename = file_path.split("/")[-1]
        is_remote = True
    elif os.path.isabs(filename):
        # Validate that the file exists
        if check_file_exists(config, filename) is False:
            logging.error(
                'File not created for CSV row "%s": file "%s" does not exist.',
                node_csv_row[config["id_field"]],
                filename,
            )
            return False
        file_path = filename
    else:
        if check_file_exists(config, filename) is False:
            logging.error(
                'File not created for CSV row "%s": file "%s" does not exist.',
                node_csv_row[config["id_field"]],
                filename,
            )
            return False
        file_path = os.path.join(config["input_dir"], filename)

    media_type = set_media_type(config, file_path, file_fieldname, node_csv_row)

    if media_type in config["media_type_file_fields"]:
        media_file_field = config["media_type_file_fields"][media_type]
    else:
        logging.error(
            'File not created for CSV row "%s": media type "%s" not recognized.',
            node_csv_row[config["id_field"]],
            media_type,
        )
        return False

    # Requests/urllib3 requires filenames used in Content-Disposition headers to be encoded as latin-1.
    # Since it is impossible to reliably convert to latin-1 without knowing the source encoding of the filename
    # (which may or may not have originated on the machine running Workbench, so sys.stdout.encoding isn't reliable),
    # the best we can do for now is to use unidecode to replace non-ASCII characters in filenames with their ASCII
    # equivalents (at least the unidecode() equivalents). Also, while Requests requires filenames to be encoded
    # in latin-1, Drupal passes filenames through its validateUtf8() function. So ASCII is a low common denominator
    # of both requirements.
    # TODO: Requests supports RFC 5587 which allows for ISO-8859-1 (latin-1) and UTF-8 filenames in Content-Disposition headers.
    #   see: https://www.rfc-editor.org/rfc/rfc5987#section-3.2.2
    original_filename = copy.copy(filename)
    ascii_only = string_is_ascii(filename)
    if ascii_only is False:
        filename = unidecode(filename)
        logging.warning(
            "Filename '"
            + original_filename
            + "' contains non-ASCII characters, normalized to '"
            + filename
            + "'."
        )

    file_endpoint_path = (
        "/file/upload/media/" + media_type + "/" + media_file_field + "?_format=json"
    )
    if config["keep_filename_parent_directory"] is True:
        remote_filename = filename
    else:
        remote_filename = os.path.basename(filename)
    file_headers = {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": 'file; filename="'
        + remote_filename
        + "\"; filename*=utf-8''"
        + urllib.parse.quote_plus(original_filename),
    }

    binary_data = open(file_path, "rb")

    try:
        file_response = issue_request(
            config, "POST", file_endpoint_path, file_headers, "", binary_data
        )
        if file_response.status_code == 201:
            file_json = json.loads(file_response.text)
            file_id = file_json["fid"][0]["value"]
            # For now, we can only validate checksums for files named in the 'file' CSV column.
            # See https://github.com/mjordan/islandora_workbench/issues/307.
            if config["fixity_algorithm"] is not None and file_fieldname == "file":
                file_uuid = file_json["uuid"][0]["value"]
                hash_from_drupal = get_file_hash_from_drupal(
                    config, file_uuid, config["fixity_algorithm"]
                )
                hash_from_local = get_file_hash_from_local(file_path, config["fixity_algorithm"])
                if hash_from_drupal == hash_from_local:
                    logging.info(
                        'Local and Drupal %s checksums for file "%s" (%s) match.',
                        config["fixity_algorithm"],
                        file_path,
                        hash_from_local,
                    )
                else:
                    print(
                        "Warning: local and Drupal checksums for '"
                        + file_path
                        + "' do not match. See the log for more detail."
                    )
                    logging.warning(
                        'Local and Drupal %s checksums for file "%s" (named in CSV row "%s") do not match (local: %s, Drupal: %s).',
                        config["fixity_algorithm"],
                        file_path,
                        node_csv_row[config["id_field"]],
                        hash_from_local,
                        hash_from_drupal,
                    )
                if "checksum" in node_csv_row:
                    if hash_from_local == node_csv_row["checksum"].strip():
                        logging.info(
                            'Local %s checksum and value in the CSV "checksum" field for file "%s" (%s) match.',
                            config["fixity_algorithm"],
                            file_path,
                            hash_from_local,
                        )
                    else:
                        print(
                            "Warning: local checksum and value in CSV for '"
                            + file_path
                            + "' do not match. See the log for more detail."
                        )
                        logging.warning(
                            'Local %s checksum and value in the CSV "checksum" field for file "%s" (named in CSV row "%s") do not match (local: %s, CSV: %s).',
                            config["fixity_algorithm"],
                            file_path,
                            node_csv_row[config["id_field"]],
                            hash_from_local,
                            node_csv_row["checksum"],
                        )
            if is_remote and config["delete_tmp_upload"] is True:
                containing_folder = os.path.join(
                    config["temp_dir"],
                    re.sub("[^A-Za-z0-9]+", "_", node_csv_row[config["id_field"]]),
                )
                try:
                    # E.g., on Windows, "[WinError 32] The process cannot access the file because it is being used by another process"
                    shutil.rmtree(containing_folder)
                except PermissionError as e:
                    logging.error(e)

            return file_id
        else:
            logging.error(
                'File not created for "'
                + file_path
                + '", POST request to "%s" returned an HTTP status code of "%s" and a response body of %s.',
                file_endpoint_path,
                file_response.status_code,
                file_response.content,
            )
            return False
    except requests.exceptions.RequestException as e:
        logging.error(e)
        return False


def get_term_field_values(config: dict, term_id: str):
    """Get a term's field data so we can use it during PATCH updates,
    which replace a field's values.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param term_id: string - The term ID whose field data is being requested.
    """
    url = config["host"] + "/taxonomy/term/" + term_id + "?_format=json"
    response = issue_request(config, "GET", url)
    term_fields = response.json()
    return term_fields


def download_file_from_drupal(
    config: dict,
    node_id: str,
    media_use_term_id: Optional[str] = None,
    media_list: Optional[list] = None,
) -> Union[bool, str]:
    """Download a media file from Drupal.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param node_id: str - The node ID to download the media file for.
    :param media_use_term_id: str|None - The media use term ID or URI
    :param media_list: list|None - An optional pre-fetched media list.
    :return: str|bool - The path to the downloaded file, or False on failure
    """
    if config.get("export_file_directory") is None:
        logging.error("export_file_directory is not configured")
        return False

    if not os.path.exists(config["export_file_directory"]):
        try:
            os.mkdir(config["export_file_directory"])
        except Exception as e:
            message = f'Path "export_file_directory" ("{config["export_file_directory"]}") is not writable: {str(e)}'
            logging.error(message)
            sys.exit("Error: " + message + " See log for more detail.")
    else:
        logging.info(
            f'Path "export_file_directory" ("{config["export_file_directory"]}") already exists.'
        )

    media_list = get_media_list(config, node_id, media_list)
    if media_list is None:
        return False

    resolved_term_id = resolve_media_use_term_id(config, media_use_term_id, node_id)
    if resolved_term_id is None:
        return False

    file_url = find_file_url_in_media(config, media_list, resolved_term_id, node_id)
    if not file_url:
        return False

    url_filename = os.path.basename(urllib.parse.urlparse(file_url).path)
    downloaded_file_path = os.path.join(config["export_file_directory"], url_filename)
    if os.path.exists(downloaded_file_path):
        downloaded_file_path = get_deduped_file_path(downloaded_file_path)

    try:
        with open(downloaded_file_path, "wb+") as f:
            file_download_response = requests.get(
                file_url,
                allow_redirects=True,
                verify=config["secure_ssl_only"],
                headers={"User-Agent": config["user_agent"]},
            )
            if file_download_response.status_code == 200:
                # TODO: Stream the download to avoid loading the entire file into memory.
                f.write(file_download_response.content)
                filename_for_logging = os.path.basename(downloaded_file_path)
                logging.info(
                    f'File "{filename_for_logging}" downloaded for node {node_id}.'
                )
                return (
                    downloaded_file_path
                    if os.path.isabs(config["export_file_directory"])
                    else filename_for_logging
                )
            else:
                logging.error(
                    f"File download failed for node {node_id}: {file_url} (HTTP {file_download_response.status_code})"
                )
                return False
    except Exception as e:
        logging.error(f"File download failed for node {node_id}: {str(e)}")
        return False


