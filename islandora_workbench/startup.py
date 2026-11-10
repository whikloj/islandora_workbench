import argparse
import json
import logging
import sys
import time
from argparse import Namespace

import requests

from islandora_workbench.config import WorkbenchConfig, print_config, do_overrides
from islandora_workbench.context import initialize_context
from islandora_workbench.drupal.client import get_integration_module_version, ping_islandora, \
    get_mid_from_media_url_alias, remove_media_and_file, issue_request
from islandora_workbench.drupal.utils import get_nid_from_url_alias
from islandora_workbench.tasks import get_task
from islandora_workbench.utils.string_utils import value_is_numeric


def startup(args: argparse.Namespace):
    """
    Startup function to initialize the application context and logger.
    :param args: The command-line arguments parsed by argparse.
    :return: A dictionary containing the application context and logger.
    """
    config = WorkbenchConfig(args)
    context = initialize_context(config.get_config(), config.get_logger())
    do_overrides(context, args)

    if args.print_config:
        print_config(context)
        return

    ping_islandora(context["config"], print_message=False)

    context["EXECUTION_START_TIME"] = time.time()
    context["INTEGRATION_MODULE_VERSION"] = get_integration_module_version(context["config"])

    if args.quick_delete_node:
        quick_delete_node(context["config"], args)
    elif args.quick_delete_media:
        quick_delete_media(context["config"], args)

    task = get_task(context)
    if task.is_check_mode:
        task.check()
    else:
        task.run()


def quick_delete_media(config: dict, args: Namespace):
    """Delete a media entity quickly by its media ID or URL alias.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param args: Namespace - The command-line arguments passed to Workbench.
    """
    logging.info("--quick_delete_mediatask started for " + args.quick_delete_media)

    if config["standalone_media_url"] is False and not args.quick_delete_media.endswith(
        "/edit"
    ):
        message = f"You need to add '/edit' to the end of your media URL (e.g. {args.quick_delete_media}/edit)."
        logging.error(message)
        sys.exit("Error: " + message)

    if config["standalone_media_url"] is True and args.quick_delete_media.endswith(
        "/edit"
    ):
        message = f"You need to remove '/edit' to the end of your media URL."
        logging.error(message)
        sys.exit("Error: " + message)

    ping_response = issue_request(
        config, "GET", args.quick_delete_media + "?_format=json"
    )
    if ping_response.status_code == 404:
        message = f"Cannot find {args.quick_delete_media}. Please verify the media URL and try again."
        logging.error(message + f"HTTP response code was {ping_response.status_code}.")
        sys.exit("Error: " + message)

    try:
        entity = ping_response.json()
        if "mid" not in entity:
            message = f"{args.quick_delete_media} does not apear to be a media."
            logging.error(message)
            sys.exit("Error: " + message)
    except requests.exceptions.JSONDecodeError:
        message = f"Could not decode JSON response from {args.quick_delete_media}."
        logging.error(message)
        sys.exit("Error: " + message)

    media_id = get_mid_from_media_url_alias(config, args.quick_delete_media)
    media_delete_status_code = remove_media_and_file(config, media_id)
    if media_delete_status_code == 204:
        message = f"Media {args.quick_delete_media} and associated file deleted."
        print(message)
        logging.info(message)
    else:
        message = f"Media {args.quick_delete_media} and associated file not deleted. See Workbench log for more detail."
        print("Error: " + message)
        logging.error(message + f"HTTP response code was {media_delete_status_code}.")

    sys.exit()


def quick_delete_node(config: dict, args: Namespace):
    """Delete a node quickly by its node ID or URL alias.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param args: Namespace - The command-line arguments passed to Workbench.
    """
    logging.info("--quick_delete_node task started for " + args.quick_delete_node)

    if value_is_numeric(args.quick_delete_node) is True:
        response = issue_request(
            config, "GET", args.quick_delete_node + "?_format=json"
        )
        if response.status_code != 200:
            message = f"Sorry, {args.quick_delete_node} can't be accessed. Please confirm the node exists and is accessible to the user defined in your Workbench configuration."
            logging.error(
                message + f" (HTTP response code was {response.status_code}.)"
            )
            sys.exit("Error: " + message)
    else:
        node_id = get_nid_from_url_alias(config, args.quick_delete_node)
        if node_id is False:
            message = f"Sorry, {args.quick_delete_node} can't be accessed. Please confirm the node exists and is accessible to the user defined in your Workbench configuration."
            logging.error(message)
            sys.exit("Error: " + message)
        else:
            response = issue_request(
                config, "GET", f'{config["host"]}/node/{node_id}' + "?_format=json"
            )
            if response.status_code != 200:
                message = f"Sorry, {args.quick_delete_node} can't be accessed. Please confirm the node exists and is accessible to the user defined in your Workbench configuration."
                logging.error(
                    message + f" (HTTP response code was {response.status_code}.)"
                )
                sys.exit("Error: " + message)
    try:
        entity = response.json()
        if "type" in entity:
            if entity["type"][0]["target_type"] == "node_type":
                # Delete the node's media first.
                if config["delete_media_with_nodes"] is True:
                    media_endpoint = (
                        config["host"] + "/node/" + str(node_id) + "/media?_format=json"
                    )
                    media_response = issue_request(config, "GET", media_endpoint)
                    media_response_body = json.loads(media_response.text)
                    media_messages = []
                    for media in media_response_body:
                        if "mid" in media:
                            media_id = media["mid"][0]["value"]
                            media_delete_status_code = remove_media_and_file(
                                config, media_id
                            )
                            if media_delete_status_code == 204:
                                media_messages.append(
                                    "+ Media "
                                    + config["host"]
                                    + "/media/"
                                    + str(media_id)
                                    + " deleted."
                                )

                # Then the node.
                node_endpoint = (
                    config["host"] + "/node/" + str(node_id) + "?_format=json"
                )
                node_response = issue_request(config, "DELETE", node_endpoint)
                if node_response.status_code == 204:
                    if config["progress_bar"] is False:
                        print("Node " + args.quick_delete_node + " deleted.")
                    logging.info("Node %s deleted.", args.quick_delete_node)
                if (
                    config["delete_media_with_nodes"] is True
                    and config["progress_bar"] is False
                ):
                    if len(media_messages):
                        for media_message in media_messages:
                            print(media_message)
            else:
                message = f"{args.quick_delete_node} does not apear to be a node."
                logging.error(message)
                sys.exit("Error: " + message)
        else:
            message = f"{args.quick_delete_node} does not apear to be a node."
            logging.error(message)
            sys.exit("Error: " + message)
    except requests.exceptions.JSONDecodeError:
        message = f"Could not decode JSON response from {args.quick_delete_node}."
        logging.error(message)
        sys.exit("Error: " + message)

    sys.exit()
