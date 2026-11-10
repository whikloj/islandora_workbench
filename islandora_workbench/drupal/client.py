import copy
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.parse
from typing import Union, Literal, OrderedDict, Optional
from urllib.parse import urlparse

import requests
from cachetools import cached, LFUCache
from cachetools.keys import hashkey
from requests.adapters import HTTPAdapter
from unidecode import unidecode
from urllib3 import Retry

from islandora_workbench.checks.validation import validate_media_track_value
from islandora_workbench.exceptions import WorkbenchException
from islandora_workbench.utils import value_is_numeric, integration_supports_field_bundle, get_mimetype_from_extension, \
    split_media_track_string, check_file_exists
from workbench_utils import set_media_type, get_oembed_media_types, file_fields, ping_remote_file
from islandora_workbench.drupal.media import resolve_media_use_term_id, get_media_list
from islandora_workbench.external_scripts import execute_entity_post_task_script
from islandora_workbench.utils.string_utils import clean_image_alt_text, get_deduped_file_path
from islandora_workbench.utils.remote_utils import get_preprocessed_file_path, download_remote_file
from islandora_workbench.drupal.utils import get_nid_from_url_alias, get_node_title_from_nid

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
) -> Union[bool, str]:
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
     boolean|str
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
            return response.text
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
    if url_alias.startswith("http://") and config["host"].startswith("https://"):
        url_alias = re.sub(r"^http://", "https://", url_alias, flags=re.IGNORECASE)

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

def create_media(
    config: dict,
    filename: str,
    file_fieldname: Union[str, None],
    node_id: str,
    csv_row: dict,
    media_use_tid: Union[int, str, None] = None,
) -> Union[int, bool, None]:
    """Creates a media in Drupal.

    Parameters
    ----------
     config : dict
         The configuration settings defined by workbench_config.get_config().
     filename : string
         The value of the CSV 'file' field for the current node.
     file_fieldname: string|None
         The name of the CSV column containing the filename. None if the file isn't
         in a CSV field (e.g., when config['paged_content_from_directories'] is True
         or config["paged_content_from_directories_parents_exist"] is True).
     node_id: string
         The ID or URL alias of the node to attach the media to. This is False if file creation failed.
     csv_row: dict
         E.g., dict([('file', 'IMG_5083.JPG'), ('id', '05'), ('title', 'Alcatraz Island').
         Could be either a CSV row describing nodes (e.g. during 'create' tasks) or describing
         media (e.g. during 'add_media' tasks).
     media_use_tid : int|str
         A valid term ID (or a subdelimited list of IDs) from the Islandora Media Use vocabulary.
     Returns
     -------
     int|False|None
          The HTTP status code from the attempt to create the media, False if
          it doesn't have sufficient information to create the media, or None
          if config['nodes_only'] is True.
    """
    if config["nodes_only"] is True:
        return None

    if len(filename.strip()) == 0:
        if file_fieldname is None:
            # TODO: file_fieldname is None but we are using it in the message below.
            message = (
                'Media not created because field "'
                + file_fieldname
                + '" in CSV row with ID "'
                + csv_row[config["id_field"]]
                + '" is empty.'
            )
            logging.error(message)
            return False

    if check_file_exists(config, filename) is False:
        if file_fieldname is None:
            message = (
                'Media not created because file "' + filename + '" could not be found.'
            )
        else:
            message = (
                'Media not created because file "'
                + filename
                + '" identified in field "'
                + file_fieldname
                + '" in CSV row with ID "'
                + csv_row[config["id_field"]]
                + '" could not be found.'
            )
        logging.error(message)
        return False

    # Importing the workbench_fields module at the top of this module with the
    # rest of the imports causes a circular import exception, so we do it here.

    if value_is_numeric(node_id) is False:
        node_id = get_nid_from_url_alias(config, node_id)

    # media_type_for_oembed_check = set_media_type(config, filename, file_fieldname, node_csv_row)
    media_type = set_media_type(config, filename, file_fieldname, csv_row)

    if media_type in get_oembed_media_types(config):
        # file_result must be an integer.
        file_result = -1
    else:
        file_result = create_file(config, filename, file_fieldname, csv_row, node_id)

    if filename.startswith("http"):
        if file_result > 0:
            filename = get_preprocessed_file_path(
                config, file_fieldname, csv_row, node_id, False
            )

    if isinstance(file_result, int):
        if "media_use_tid" in csv_row and len(csv_row["media_use_tid"]) > 0:
            media_use_tid_value = csv_row["media_use_tid"]
        else:
            media_use_tid_value = config["media_use_tid"]

        if media_use_tid is not None:
            media_use_tid_value = media_use_tid

        media_use_tids = []
        media_use_terms = str(media_use_tid_value).split(config["subdelimiter"])
        for media_use_term in media_use_terms:
            if value_is_numeric(media_use_term):
                media_use_tids.append(media_use_term)
            if not value_is_numeric(
                media_use_term
            ) and media_use_term.strip().startswith("http"):
                media_use_tids.append(get_term_id_from_uri(config, media_use_term))
            if not value_is_numeric(
                media_use_term
            ) and not media_use_term.strip().startswith("http"):
                media_use_tids.append(
                    find_term_in_vocab(
                        config, "islandora_media_use", media_use_term.strip()
                    )
                )

        media_bundle_response_code = ping_media_bundle(config, media_type)
        if media_bundle_response_code == 404:
            message = (
                'File "'
                + filename
                + '" identified in CSV row '
                + file_fieldname
                + " will create a media of type ("
                + media_type
                + "), but that media type is not configured in the destination Drupal."
            )
            logging.error(message)
            return False

        media_field = config["media_type_file_fields"][media_type]
        if media_type in get_oembed_media_types(config):
            if "title" in csv_row:
                # WIP on #572: 'title' applies to node CSVs, for media, it should be 'name'.
                media_name = csv_row["title"]
            else:
                media_name = get_node_title_from_nid(config, node_id)
                if not media_name:
                    message = 'Cannot access node " + node_id + ", so cannot get its title for use in media title. Using oEmbed URL instead.'
                    logging.warning(message)
                    media_name = os.path.basename(filename)
        else:
            media_name = os.path.basename(filename)

        if config["use_node_title_for_media_title"]:
            if "title" in csv_row:
                # WIP on #572: 'title' applies to node CSVs, for media, it should be 'name'.
                media_name = csv_row["title"]
            else:
                media_name = get_node_title_from_nid(config, node_id)
                if not media_name:
                    message = 'Cannot access node " + node_id + ", so cannot get its title for use in media title. Using filename instead.'
                    logging.warning(message)
                    media_name = os.path.basename(filename)
        elif config["use_nid_in_media_title"]:
            media_name = f"{node_id}-Original File"
        elif config["field_for_media_title"]:
            if len(csv_row[config["field_for_media_title"]]) > 0:
                media_name = csv_row[config["field_for_media_title"]][:255]
        else:
            media_name = os.path.basename(filename)

        # Create a media from an oEmbed URL.
        if media_type in get_oembed_media_types(config):
            media_json = {
                "bundle": [
                    {
                        "target_id": media_type,
                        "target_type": "media_type",
                    }
                ],
                "name": [{"value": media_name}],
                media_field: [{"value": filename}],
                "field_media_of": [{"target_id": int(node_id), "target_type": "node"}],
                "field_media_use": [
                    {"target_id": media_use_tids[0], "target_type": "taxonomy_term"}
                ],
            }
        # Create a media from a local or remote file.
        else:
            media_json = {
                "bundle": [
                    {
                        "target_id": media_type,
                        "target_type": "media_type",
                    }
                ],
                "name": [{"value": media_name}],
                media_field: [{"target_id": file_result, "target_type": "file"}],
                "field_media_of": [{"target_id": int(node_id), "target_type": "node"}],
                "field_media_use": [
                    {"target_id": media_use_tids[0], "target_type": "taxonomy_term"}
                ],
            }

            # Use the 'paged_content_additional_page_media' config setting to determine
            # if any hOCR files are being added, since we need to explicitly define hOCR
            # media's MIME type as "text/vnd.hocr+html".
            file_is_hocr = False
            if "paged_content_additional_page_media" in config:
                file_mimetype = get_mimetype_from_extension(config, filename)
                for uri_to_extension_mapping in config[
                    "paged_content_additional_page_media"
                ]:
                    if (
                        "https://discoverygarden.ca/use#hocr"
                        in uri_to_extension_mapping
                    ):
                        file_is_hocr = True

            if file_is_hocr is True:
                media_use_uri = get_term_uri(config, media_use_tids[0])
                if (
                    media_use_uri == "https://discoverygarden.ca/use#hocr"
                    and file_mimetype == "text/vnd.hocr+html"
                ):
                    media_json.update({"field_mime_type": [{"value": file_mimetype}]})

        if "published" in csv_row and len(csv_row["published"]) > 0:
            media_json["status"] = {"value": csv_row["published"]}

        # Populate some media type-specific fields on the media. @todo: We need a generalized way of
        # determining which media fields are required, e.g. checking the media type configuration.
        if media_field == "field_media_image":
            if "image_alt_text" in csv_row and len(csv_row["image_alt_text"]) > 0:
                alt_text = clean_image_alt_text(csv_row["image_alt_text"])
                media_json[media_field][0]["alt"] = alt_text
            else:
                alt_text = clean_image_alt_text(media_name)
                media_json[media_field][0]["alt"] = alt_text

        # extracted_text media must have their field_edited_text field populated for full text indexing.
        # Text must be encoded as utf-8.
        if media_type == "extracted_text":
            if check_file_exists(config, filename):
                media_json["field_edited_text"] = list()
                if os.path.isabs(filename) is False:
                    filename = os.path.join(config["input_dir"], filename)
                try:
                    extracted_text_file = open(filename, "r", -1, "utf-8-sig")
                    media_json["field_edited_text"].append(
                        {"value": extracted_text_file.read()}
                    )
                except Exception as e:
                    logging.error(
                        f'Extracted text file "{filename}" caused a problem that prevented it from being ingested ({e}).'
                    )
            else:
                logging.error("Extracted text file %s not found.", filename)

        # WIP on #572: if this is an `add_media` task, add fields in CSV to media_json, being careful to
        # not stomp on existing fields. Block below is copied from create() and needs to be modified to
        # suit creation of custom fields in add_media tasks.
        """
        if config['task'] == 'add_media':
            field_definitions = get_field_definitions(config, 'media')

            # Add custom (non-required) CSV fields.
            entity_fields = get_entity_fields(config, 'node', config['content_type'])
            # Only add config['id_field'] to required_fields if it is not a node field.
            required_fields = ['file', 'title']
            if config['id_field'] not in entity_fields:
                required_fields.append(config['id_field'])
            custom_fields = list(set(csv_column_headers) - set(required_fields))
            additional_files_entries = get_additional_files_config(config)
            for custom_field in custom_fields:
                # Skip processing field if empty.
                if len(row[custom_field].strip()) == 0:
                    continue

                if len(additional_files_entries) > 0:
                    if custom_field in additional_files_entries.keys():
                        continue

                # This field can exist in the CSV to create parent/child
                # relationships and is not a Drupal field.
                if custom_field == 'parent_id':
                    continue

                # 'langcode' is a core Drupal field, but is not considered a "base field".
                if custom_field == 'langcode':
                    continue

                # 'image_alt_text' is a reserved CSV field.
                if custom_field == 'image_alt_text':
                    continue

                # 'url_alias' is a reserved CSV field.
                if custom_field == 'url_alias':
                    continue

                # 'media_use_tid' is a reserved CSV field.
                if custom_field == 'media_use_tid':
                    continue

                # 'checksum' is a reserved CSV field.
                if custom_field == 'checksum':
                    continue

                # We skip CSV columns whose headers use the 'media:video:field_foo' media track convention.
                if custom_field.startswith('media:'):
                    continue

                # Assemble Drupal field structures from CSV data. If new field types are added to
                # fields.py, they need to be registered in the following if/elif/else block.

                # Entity reference fields (taxonomy_term and node).
                if field_definitions[custom_field]['field_type'] == 'entity_reference':
                    entity_reference_field = workbench_fields.EntityReferenceField()
                    node = entity_reference_field.create(config, field_definitions, node, row, custom_field)

                # Typed relation fields.
                elif field_definitions[custom_field]['field_type'] == 'typed_relation':
                    typed_relation_field = workbench_fields.TypedRelationField()
                    node = typed_relation_field.create(config, field_definitions, node, row, custom_field)

                # Geolocation fields.
                elif field_definitions[custom_field]['field_type'] == 'geolocation':
                    geolocation_field = workbench_fields.GeolocationField()
                    node = geolocation_field.create(config, field_definitions, node, row, custom_field)

                # Link fields.
                elif field_definitions[custom_field]['field_type'] == 'link':
                    link_field = workbench_fields.LinkField()
                    node = link_field.create(config, field_definitions, node, row, custom_field)

                # Authority Link fields.
                elif field_definitions[custom_field]['field_type'] == 'authority_link':
                    link_field = workbench_fields.AuthorityLinkField()
                    node = link_field.create(config, field_definitions, node, row, custom_field)

                # For non-entity reference and non-typed relation fields (text, integer, boolean etc.).
                else:
                    simple_field = workbench_fields.SimpleField()
                    node = simple_field.create(config, field_definitions, node, row, custom_field)
        """

        # Create media_track files here, since they should exist before we create the parent media.
        # @todo WIP on #572: if there are track file fields in the add_media CSV, create them here, as below for track file field in node CSV.
        media_types_with_track_files = config["media_track_file_fields"].keys()
        valid_media_track_fields = list()
        if media_type in media_types_with_track_files:
            # Check for fields in node_csv_row that have names like 'media:video:field_track' and validate their contents.
            # Note: Does not validate the fields' configuration (--check does that).
            node_csv_field_names = list(csv_row.keys())
            if len(node_csv_field_names):
                media_track_fields = [
                    x
                    for x in node_csv_field_names
                    if x.startswith("media:" + media_type)
                ]
                # Should be just one field per media type.
                if (
                    len(media_track_fields)
                    and media_type in config["media_track_file_fields"]
                ):
                    for media_track_field in media_track_fields:
                        if (
                            validate_media_track_value(csv_row[media_track_field])
                            is True
                        ):
                            valid_media_track_fields.append(media_track_field)

            # Create the media track file(s) for each entry in valid_potential_media_track_fields (there could be multiple track entries).
            if len(valid_media_track_fields):
                media_track_field_data = []
                # Should be just one field per media type.
                fully_qualified_media_track_field_name = valid_media_track_fields[0]
                media_track_entries = split_media_track_string(
                    config, csv_row[fully_qualified_media_track_field_name]
                )
                for media_track_entry in media_track_entries:
                    media_track_field_name_parts = (
                        fully_qualified_media_track_field_name.split(":")
                    )
                    try:
                        create_track_file_result = create_file(
                            config,
                            media_track_entry["file_path"],
                            fully_qualified_media_track_field_name,
                            csv_row,
                            node_id,
                        )
                    except Exception as e:
                        media_track_entry_file_path = media_track_entry["file_path"]
                        logging.error(
                            f'Media track file "{media_track_entry_file_path}" caused a problem that prevented it from being ingested ({e}).'
                        )
                        continue

                    if create_track_file_result is not False and isinstance(
                        create_track_file_result, int
                    ):
                        # /entity/file/xxx?_format=json will return JSON containing the file's 'uri'.
                        track_file_info_response = issue_request(
                            config,
                            "GET",
                            f"/entity/file/{create_track_file_result}?_format=json",
                        )
                        track_file_info = json.loads(track_file_info_response.text)
                        track_file_url = track_file_info["uri"][0]["url"]
                        logging.info(
                            f"Media track file {config['host'].rstrip('/')}{track_file_url} created from {media_track_entry['file_path']}."
                        )
                        track_file_data = {
                            "target_id": track_file_info["fid"][0]["value"],
                            "kind": media_track_entry["kind"],
                            "label": media_track_entry["label"],
                            "srclang": media_track_entry["srclang"],
                            "default": False,
                            "url": track_file_url,
                        }
                        media_track_field_data.append(track_file_data)
                    else:
                        # If there are any failures, proceed with creating the parent media.
                        logging.error(
                            f"Media track using {media_track_entry['file_path']} not created; create_file returned {create_track_file_result}."
                        )

                    # Set the "default" attribute of the first media track.
                    if media_track_field_data:
                        media_track_field_data[0]["default"] = True
                        media_json[media_track_field_name_parts[2]] = (
                            media_track_field_data
                        )

        media_endpoint_path = (
            "/entity/media?_format=json"
            if config["standalone_media_url"]
            else "/entity/media"
        )
        media_headers = {"Content-Type": "application/json"}
        try:
            media_response = issue_request(
                config, "POST", media_endpoint_path, media_headers, media_json
            )
            if media_response.status_code != 201:
                logging.error(
                    'Media not created, POST request to "%s" returned an HTTP status code of "%s" and a response body of %s.',
                    media_endpoint_path,
                    media_response.status_code,
                    media_response.content,
                )
                logging.error(
                    'JSON request body used in previous POST to "%s" was %s.',
                    media_endpoint_path,
                    media_json,
                )

            if len(media_use_tids) > 1:
                media_response_body = json.loads(media_response.text)
                if "mid" in media_response_body:
                    media_id = media_response_body["mid"][0]["value"]
                    patch_media_use_terms(config, media_id, media_type, media_use_tids)
                else:
                    logging.error(
                        "Could not PATCH additional media use terms to media created from '%s' because media ID is not available.",
                        filename,
                    )

            # Execute media-specific post-create scripts, if any are configured.
            if "media_post_create" in config and len(config["media_post_create"]) > 0:
                for command in config["media_post_create"]:
                    post_task_output, post_task_return_code = (
                        execute_entity_post_task_script(
                            command,
                            config["config_file_path"],
                            media_response.status_code,
                            media_response.text,
                            os.path.abspath(filename),
                        )
                    )
                    if post_task_return_code == 0:
                        logging.info(
                            "Post media create script "
                            + command
                            + " executed successfully."
                        )
                    else:
                        logging.error(
                            "Post media create script " + command + " failed."
                        )

            return media_response.status_code
        except requests.exceptions.RequestException as e:
            logging.error(e)
            return False

    elif file_result is False or file_result is None:
        return file_result


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


def get_node_media_ids(
    config: dict, node_id: str, media_use_tids: list = None, media_type: str = None
) -> Union[bool, list]:
    """Gets a list of media IDs for a node.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        node_id : string
            The ID of the node to delete media from.
        media_use_tids : list
            Term IDs from the Islandora Media Use vocabulary. If present, media
            with one of these tids will be added to the returned list of media IDs.
            If empty, all media will be included in the returned list of media IDs.
        media_type: string
            A media type machine name to filter on.
        Returns
        -------
        list|bool
            List of media IDs, or False if there was a problem fetching the list of media.
    """
    if media_use_tids is None:
        media_use_tids = []

    # Convert entries in media_use_tids from term names or URIs into term IDs.
    if len(media_use_tids) > 0:
        for i in range(len(media_use_tids)):
            if (
                value_is_numeric(media_use_tids[i]) is False
                and media_use_tids[i].strip().startswith("http") is True
            ):
                media_use_tid_info = get_all_representations_of_term(
                    config,
                    vocab_id="islandora_media_use",
                    uri=media_use_tids[i],
                )
                media_use_tids[i] = media_use_tid_info["term_id"]
            elif (
                    value_is_numeric(media_use_tids[i]) is False
                    and media_use_tids[i].strip().startswith("http") is False
            ):
                media_use_tid_info = get_all_representations_of_term(
                    config,
                    vocab_id="islandora_media_use",
                    name=media_use_tids[i],
                )
                media_use_tids[i] = media_use_tid_info["term_id"]
            else:
                media_use_tids[i] = media_use_tids[i]

    media_id_list = list()
    url = f"{config['host']}/node/{node_id}/media?_format=json"
    response = issue_request(config, "GET", url)
    if response.status_code == 200:
        body = response.json()
        for media in body:
            if media_type is not None:
                if media_type != media["bundle"][0]["target_id"]:
                    continue
            if len(media_use_tids) == 0:
                media_id_list.append(media["mid"][0]["value"])
            else:
                for media_use_tid_json in media["field_media_use"]:
                    if media_use_tid_json["target_id"] in media_use_tids:
                        media_id_list.append(media["mid"][0]["value"])
        return media_id_list
    else:
        message = f"Attempt to get media list for node ID {node_id} returned a {response.status_code} status code."
        print("Error: " + message)
        logging.warning(message)
        return False


def remove_media_and_file(config: dict, media_id: Union[int, str]) -> Union[int, bool]:
    """Delete a media and the file associated with it.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param media_id: int|str - The media ID to patch.
    :return: int|bool - The HTTP status code from the DELETE request for the media, or False on failure.
    """
    # First get the media JSON.
    if config["standalone_media_url"] is True:
        get_media_url = config["host"] + "/media/" + str(media_id) + "?_format=json"
    else:
        get_media_url = (
            config["host"] + "/media/" + str(media_id) + "/edit?_format=json"
        )
    get_media_response = issue_request(config, "GET", get_media_url)
    get_media_response_body = json.loads(get_media_response.text)

    # See https://github.com/mjordan/islandora_workbench/issues/446 for background.
    if "message" in get_media_response_body and get_media_response_body[
        "message"
    ].startswith("No route found for"):
        message = f'Please visit {config["host"]}/admin/config/media/media-settings and uncheck the "Standalone media URL" option.'
        logging.error(message)
        sys.exit("Error: " + message)

    # See https://github.com/mjordan/islandora_workbench/issues/446 for background.
    if get_media_response.status_code == 403:
        message = (
            f'If the "Standalone media URL" option at {config["host"]}/admin/config/media/media-settings is unchecked, clear your Drupal cache and run Workbench again.'
            + ' If that doesn\'t work, try adding "standalone_media_url: true" to your configuration file.'
        )
        logging.error(message)
        sys.exit("Error: " + message)

    for file_field_name in file_fields:
        if file_field_name in get_media_response_body:
            try:
                file_id = get_media_response_body[file_field_name][0]["target_id"]
            except Exception as e:
                logging.error(
                    "Unable to get file ID for media %s (reason: %s); proceeding to delete media without file.",
                    media_id,
                    e,
                )
                file_id = None
            break

    # Delete the file first.
    # TODO: file_id might be undefined here if none of the file_fields are in get_media_response_body.
    if file_id is not None:
        file_endpoint = (
            config["host"] + "/entity/file/" + str(file_id) + "?_format=json"
        )
        file_response = issue_request(config, "DELETE", file_endpoint)
        if file_response.status_code == 204:
            logging.info("File %s (from media %s) deleted.", file_id, media_id)
        else:
            logging.error(
                "File %s (from media %s) not deleted (HTTP response code %s).",
                file_id,
                media_id,
                file_response.status_code,
            )

    # Delete any audio/video media_track files.
    media_bundle_name = get_media_response_body["bundle"][0]["target_id"]
    if media_bundle_name in config["media_track_file_fields"]:
        track_file_field = config["media_track_file_fields"][media_bundle_name]
        if (
            track_file_field in get_media_response_body
            and len(get_media_response_body[track_file_field]) > 0
        ):
            for track_file in get_media_response_body[track_file_field]:
                track_file_id = track_file["target_id"]
                track_file_endpoint = (
                    config["host"]
                    + "/entity/file/"
                    + str(track_file_id)
                    + "?_format=json"
                )
                track_file_response = issue_request(
                    config, "DELETE", track_file_endpoint
                )
                if track_file_response.status_code == 204:
                    logging.info(
                        "Media track file %s (from media %s) deleted.",
                        track_file_id,
                        media_id,
                    )
                else:
                    logging.error(
                        "Media track file %s (from media %s) not deleted (HTTP response code %s).",
                        track_file_id,
                        media_id,
                        track_file_response.status_code,
                    )

    # Then the media.
    # TODO: file_response might be undefined if file_id above is undefined because of the reasons for that.
    if file_id is None or file_response.status_code == 204:
        if config["standalone_media_url"] is True:
            media_endpoint = (
                config["host"] + "/media/" + str(media_id) + "?_format=json"
            )
        else:
            media_endpoint = (
                config["host"] + "/media/" + str(media_id) + "/edit?_format=json"
            )
        media_response = issue_request(config, "DELETE", media_endpoint)
        if media_response.status_code == 204:
            logging.info("Media %s deleted.", media_id)
            return media_response.status_code
        else:
            logging.error(
                "Media %s not deleted (HTTP response code %s).",
                media_id,
                media_response.status_code,
            )
            return False

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
        get_media_endpoint = (
            config["host"] + "/media/" + str(media_id) + "?_format=json"
        )
    else:
        get_media_endpoint = (
            config["host"] + "/media/" + str(media_id) + "/edit?_format=json"
        )
    get_media_headers = {"Content-Type": "application/json"}
    get_media_response = issue_request(
        config, "GET", get_media_endpoint, get_media_headers
    )
    if get_media_response.status_code == 200:
        get_media_response_body = json.loads(get_media_response.text)
        field_media_image_target_id = get_media_response_body["field_media_image"][0][
            "target_id"
        ]
    else:
        logging.error(
            f"Media {get_media_endpoint} returned an HTTP status code of {get_media_response.status_code}."
        )
        return False

    for field_name, field_value in csv_row.items():
        if field_name == "title":
            alt_text = clean_image_alt_text(field_value)
        # "image_alt_text" can be in "create", "add_alt_text", or "update_alt_text" input CSV.
        elif field_name == "image_alt_text":
            alt_text = clean_image_alt_text(field_value)

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

    if config["standalone_media_url"] is True:
        patch_media_endpoint = (
            config["host"] + "/media/" + str(media_id) + "?_format=json"
        )
    else:
        patch_media_endpoint = (
            config["host"] + "/media/" + str(media_id) + "/edit?_format=json"
        )
    patch_media_headers = {"Content-Type": "application/json"}
    patch_media_response = issue_request(
        config, "PATCH", patch_media_endpoint, patch_media_headers, media_json
    )

    if patch_media_response.status_code != 200:
        logging.error("Alt text for image media %s not updated.", patch_media_endpoint)

    return patch_media_response.status_code


def patch_media_use_terms(
    config: dict, media_id: Union[int, str], media_type: str, media_use_tids: list
) -> None:
    """Patch the media entity's field_media_use.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param media_id: int|str - The media ID to patch.
    :param media_type: str - The media type (bundle) of the media to patch.
    :param media_use_tids: list - A list of term IDs to set on field_media_use.
    """
    media_json = {"bundle": [{"target_id": media_type}]}

    media_use_tids_json = []
    for media_use_tid in media_use_tids:
        media_use_tids_json.append(
            {"target_id": media_use_tid, "target_type": "taxonomy_term"}
        )

    media_json["field_media_use"] = media_use_tids_json
    if config["standalone_media_url"] is True:
        endpoint = config["host"] + "/media/" + str(media_id) + "?_format=json"
    else:
        endpoint = config["host"] + "/media/" + str(media_id) + "/edit?_format=json"
    headers = {"Content-Type": "application/json"}
    response = issue_request(config, "PATCH", endpoint, headers, media_json)
    if response.status_code == 200:
        logging.info("Media %s Islandora Media Use terms updated.", endpoint)
    else:
        logging.warning("Media %s Islandora Media Use terms not updated.", endpoint)


def patch_media_fields(
    config: dict, media_id: Union[int, str], media_type: str, node_csv_row: OrderedDict
) -> None:
    """Patch the media entity with base fields from the parent node.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param media_id: int|str - The media ID to patch.
    :param media_type: str - The media type (bundle) of the media to patch.
    :param node_csv_row: OrderedDict - The CSV row of the parent node.
    """
    media_json = {"bundle": [{"target_id": media_type}]}

    for field_name, field_value in node_csv_row.items():
        if field_name == "created" and len(field_value) > 0:
            media_json["created"] = [{"value": field_value}]
        if field_name == "uid" and len(field_value) > 0:
            media_json["uid"] = [{"target_id": field_value}]

    if len(media_json) > 1:
        if config["standalone_media_url"] is True:
            endpoint = config["host"] + "/media/" + str(media_id) + "?_format=json"
        else:
            endpoint = config["host"] + "/media/" + str(media_id) + "/edit?_format=json"
        headers = {"Content-Type": "application/json"}
        response = issue_request(config, "PATCH", endpoint, headers, media_json)
        if response.status_code == 200:
            logging.info("Media %s fields updated to match parent node's.", endpoint)
        else:
            logging.warning(
                "Media %s fields not updated to match parent node's.", endpoint
            )


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


def get_node_field_values(config: dict, nid: Union[int, str]) -> dict:
    """Get a node's field data so we can use it during PATCH updates, which replace a field's values."""
    if value_is_numeric(nid) is False:
        nid = get_nid_from_url_alias(config, nid)
    node_url = config["host"] + "/node/" + str(nid) + "?_format=json"
    response = issue_request(config, "GET", node_url)
    node_fields = response.json()
    return node_fields


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
    url = f"/node/{nid}/media?_format=json"
    try:
        media_use_terms = []
        response = issue_request(config, "GET", url)
        media_list = response.json()
        for media in media_list:
            for media_use_term in media["field_media_use"]:
                term_name = get_term_name(config, media_use_term["target_id"])
                media_use_terms.append(term_name)
        media_use_terms.sort()
        return "; ".join(media_use_terms).strip()
    except Exception as e:
        message = f"Getting media list for \"{config['host']}{url}\" returned an error."
        print(f"Error: {message} See log for more detail.")
        logging.error(f"{message} Detail: {e}")


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


def get_media_file_url(
    config: dict,
    node_id: str,
    media_use_term_id: Optional[str] = None,
    media_list: Optional[list] = None,
) -> Union[bool, str]:
    """Retrieve and validate the URL of a media file from Drupal.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param node_id: str - The node ID to get the media file URL for.
    :param media_use_term_id: str|None - The media use term ID or URI
    :param media_list: list|None - An optional pre-fetched media list.
    :return: str|bool - The validated file URL, or False on failure.
    """
    media_list = get_media_list(config, node_id, media_list)
    if media_list is None:
        return False

    resolved_term_id = resolve_media_use_term_id(config, media_use_term_id, node_id)
    if resolved_term_id is None:
        return False

    file_url = find_file_url_in_media(config, media_list, resolved_term_id, node_id)
    if not file_url:
        return False

    try:
        head_response = requests.head(
            file_url,
            allow_redirects=True,
            verify=config["secure_ssl_only"],
            timeout=10,
        )
        if head_response.status_code != 200:
            logging.error(
                f"URL validation failed for node {node_id}: {file_url} (HTTP {head_response.status_code})"
            )
            return False
    except Exception as e:
        logging.error(f"HEAD request failed for {file_url}: {str(e)}")
        return False

    logging.info(f"URL validated for node {node_id}: {file_url}")
    return file_url


def find_file_url_in_media(
    config: dict, media_list: list, media_use_term_id: str, node_id: str
) -> Union[None, str]:
    """Find the file URL in media entries matching the use term.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param media_list: list - The list of media entries.
    :param media_use_term_id: str - The media use term ID to match.
    :param node_id: str - The node ID for logging purposes.
    :return: str|None - The file URL, or None if not found.
    """
    for media in media_list:
        for file_field in file_fields:
            if file_field in media:
                media_use_terms = media.get("field_media_use", [])
                media_use_ids = [term.get("target_id") for term in media_use_terms]
                if media_use_term_id in media_use_ids:
                    file_info = media[file_field]
                    if len(file_info) > 0:
                        file_url = file_info[0].get("url")
                        if file_url:
                            return file_url
    logging.warning(
        f"No valid media found for node {node_id} with use term {media_use_term_id}"
    )
    return None