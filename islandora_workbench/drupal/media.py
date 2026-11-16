import json
import logging
import os
from typing import Union, OrderedDict, Optional

import requests

from islandora_workbench.checks.validation import validate_media_track_value
from islandora_workbench.drupal import FILE_FIELDS
from islandora_workbench.drupal.client import create_file, get_term_id_from_uri, find_term_in_vocab, ping_media_bundle, \
    get_term_uri, issue_request, get_all_representations_of_term
from islandora_workbench.drupal.node import get_nid_from_url_alias, get_node_title_from_nid
from islandora_workbench.exceptions import WorkbenchException
from islandora_workbench.external_scripts import execute_entity_post_task_script
from islandora_workbench.utils import check_file_exists, value_is_numeric, get_preprocessed_file_path, \
    get_mimetype_from_extension, clean_image_alt_text, split_media_track_string
from workbench_utils import set_media_type, get_oembed_media_types, file_fields


def resolve_media_use_term_id(
    config: dict, media_use_term_id: Union[str, None], node_id: str
) -> Union[None, str]:
    """Resolve media use term ID from URI or configuration.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param media_use_term_id: str|None - The media use term ID or URI.
    :param node_id: str - The node ID for logging purposes.
    :return: str|None - The resolved term ID, or None on failure.
    """
    if media_use_term_id is None:
        media_use_term_id = config.get("export_file_media_use_term_id")
        if media_use_term_id is None:
            logging.error(
                f"No media use term ID provided or configured for node {node_id}."
            )
            return None
    if isinstance(media_use_term_id, str) and media_use_term_id.startswith("http"):
        term_id = get_term_id_from_uri(config, media_use_term_id)
        if term_id is False:
            logging.error(
                f"Failed to convert URI {media_use_term_id} to term ID for node {node_id}."
            )
            return None
        return term_id
    return media_use_term_id


def get_media_list(
    config: dict, node_id: str, media_list: list = None
) -> Union[None, list]:
    """Retrieve media list for a node if not provided.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param node_id: str - The node ID to get media for.
    :param media_list: list|None - An optional pre-fetched media list.
    :return: list|None - The media list, or None on failure.
    """
    if media_list is not None:
        return media_list
    media_list_url = f"{config['host']}/node/{node_id}/media?_format=json"
    response = issue_request(config, "GET", media_list_url)
    if response.status_code != 200:
        logging.error(
            f"Media list request failed for node {node_id}: {response.status_code}"
        )
        return None
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as e:
        logging.error(f"Media query for node {node_id} failed: {e}")
        return None


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


def remove_media_and_file(ctx: dict, media_id: Union[int, str]) -> Union[int, bool]:
    """Delete a media and the file associated with it.
    Parameters
    :param ctx: dict - The configuration context for the run.
    :param media_id: int|str - The media ID to patch.
    :return: int|bool - The HTTP status code from the DELETE request for the media, or False on failure.
    :raise WorkbenchException: If the media cannot be retrieved/accessed due to configuration issues.
    """
    # First get the media JSON.
    if ctx["config"]["standalone_media_url"] is True:
        get_media_url = ctx["config"]["host"] + "/media/" + str(media_id) + "?_format=json"
    else:
        get_media_url = (
            ctx["config"]["host"] + "/media/" + str(media_id) + "/edit?_format=json"
        )
    get_media_response = issue_request(ctx["config"], "GET", get_media_url)
    get_media_response_body = get_media_response.json()

    # See https://github.com/mjordan/islandora_workbench/issues/446 for background.
    if "message" in get_media_response_body and get_media_response_body[
        "message"
    ].startswith("No route found for"):
        message = f'Please visit {ctx["config"]["host"]}/admin/config/media/media-settings and uncheck the "Standalone media URL" option.'
        ctx["log"].error(message)
        raise WorkbenchException(message)

    # See https://github.com/mjordan/islandora_workbench/issues/446 for background.
    if get_media_response.status_code == 403:
        message = (
            f'If the "Standalone media URL" option at {ctx["config"]["host"]}/admin/config/media/media-settings is unchecked, clear your Drupal cache and run Workbench again.'
            + ' If that doesn\'t work, try adding "standalone_media_url: true" to your configuration file.'
        )
        ctx["log"].error(message)
        raise WorkbenchException(message)

    file_id = None
    for file_field_name in FILE_FIELDS:
        if file_field_name in get_media_response_body:
            try:
                file_id = get_media_response_body[file_field_name][0]["target_id"]
            except Exception as e:
                ctx["log"].error(
                    f"Unable to get file ID for media {media_id} (reason: {e}); proceeding to delete media without file.",
                )
            break

    # Delete the file first.
    file_response = {}  # Define the variable as we need to check it later when deleting the media.
    if file_id is not None:
        file_endpoint = (
            ctx["config"]["host"] + "/entity/file/" + str(file_id) + "?_format=json"
        )
        file_response = issue_request(ctx["config"], "DELETE", file_endpoint)
        # Replace the requests response object with a dictionary containing only the status code and content, to make checks easier later on.
        file_response = {
            "status_code": file_response.status_code,
            "content": file_response.content,
        }
        if file_response["status_code"] == 204:
            ctx["log"].info(f"File {file_id} (from media {media_id}) deleted.")
        else:
            ctx["log"].error(
                f"File {file_id} (from media {media_id}) not deleted (HTTP response code {file_response['status_code']}).",
            )

    # Delete any audio/video media_track files.
    media_bundle_name = get_media_response_body["bundle"][0]["target_id"]
    if media_bundle_name in ctx["config"]["media_track_file_fields"]:
        track_file_field = ctx["config"]["media_track_file_fields"][media_bundle_name]
        if (
            track_file_field in get_media_response_body
            and len(get_media_response_body[track_file_field]) > 0
        ):
            for track_file in get_media_response_body[track_file_field]:
                track_file_id = track_file["target_id"]
                track_file_endpoint = f"{ctx['config']['host']}/entity/file/{track_file_id}?_format=json"
                track_file_response = issue_request(
                    ctx["config"], "DELETE", track_file_endpoint
                )
                if track_file_response.status_code == 204:
                    ctx["log"].info(
                        f"Media track file {track_file_id} (from media {media_id}) deleted."
                    )
                else:
                    ctx["log"].error(
                        f"Media track file {track_file_id} (from media {media_id}) not deleted (HTTP response code {track_file_response.status_code})."
                    )

    # Then the media.
    if file_id is None or file_response.get("status_code", 0) == 204:
        if ctx["config"]["standalone_media_url"] is True:
            media_endpoint = (
                ctx["config"]["host"] + "/media/" + str(media_id) + "?_format=json"
            )
        else:
            media_endpoint = (
                ctx["config"]["host"] + "/media/" + str(media_id) + "/edit?_format=json"
            )
        media_response = issue_request(ctx["config"], "DELETE", media_endpoint)
        if media_response.status_code == 204:
            ctx["log"].info(f"Media {media_id} deleted.")
            return media_response.status_code
        else:
            ctx["log"].error(
                f"Media {media_id} not deleted (HTTP response code {media_response.status_code})."
            )
            return False

    return False


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
    ctx: dict, media_id: Union[int, str], media_type: str, node_csv_row: OrderedDict
) -> dict:
    """Patch the media entity with base fields from the parent node.
    Parameters
    :param ctx: dict - The configuration settings and logging context.
    :param media_id: int|str - The media ID to patch.
    :param media_type: str - The media type (bundle) of the media to patch.
    :param node_csv_row: OrderedDict - The CSV row of the parent node.
    :return dict - A thin wrapper around the response from the PATCH request, containing the status code and content.
    """
    media_json = {"bundle": [{"target_id": media_type}]}

    for field_name, field_value in node_csv_row.items():
        if field_name == "created" and len(field_value) > 0:
            media_json["created"] = [{"value": field_value}]
        if field_name == "uid" and len(field_value) > 0:
            media_json["uid"] = [{"target_id": field_value}]

    return patch_media(ctx, media_id, media_json)

def patch_media(ctx: dict, media_id: Union[int, str], media_json: dict) -> dict:
    """Patch the media entity with the provided JSON.
    :param ctx: dict - The configuration settings and logging context.
    :param media_id: int|str - The media ID to patch.
    :param media_json: dict - The JSON body to patch the media with.
    :return dict - A thin wrapper around the response from the PATCH request, containing the status code and content.
    """
    return_dict = {}
    if len(media_json) > 1:
        if ctx["config"]["standalone_media_url"] is True:
            endpoint = ctx["config"]["host"] + "/media/" + str(media_id) + "?_format=json"
        else:
            endpoint = ctx["config"]["host"] + "/media/" + str(media_id) + "/edit?_format=json"
        headers = {"Content-Type": "application/json"}
        response = issue_request(ctx["config"], "PATCH", endpoint, headers, media_json)
        return_dict = {
            "status_code": response.status_code,
            "content": response.content,
        }
        if response.status_code == 200:
            ctx["log"].info(f"Media {endpoint} fields updated to match parent node's.")
        else:
            ctx["log"].warning(
                f"Media {endpoint} fields not updated to match parent node's."
            )
    return return_dict

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


def get_media(ctx: dict, media_id: Union[str, int]) -> dict:
    """Retrieve the JSON body of a media entity from Drupal.
    Parameters
    :param ctx: dict - The configuration settings and logging context.
    :param media_id: str|int - The media ID to retrieve.
    :return: dict - The JSON body of the media entity, or an empty dict on failure.
    """
    if ctx["config"]["standalone_media_url"]:
        endpoint = f"{ctx['config']['host']}/media/{media_id}?_format=json"
    else:
        endpoint = f"{ctx['config']['host']}/media/{media_id}/edit?_format=json"

    response = issue_request(ctx["config"], "GET", endpoint)
    if response.status_code == 200:
        return response.json()
    else:
        logging.error(
            f"Failed to retrieve media {media_id} from {endpoint} (HTTP {response.status_code})"
        )
        return {}

def delete_media_file(ctx: dict, media_id: Union[str, int], file_id: Union[str, int]) -> bool:
    """Delete the file associated with a media entity in Drupal.
    Parameters
    :param ctx: dict - The configuration settings and logging context.
    :param media_id: str|int - The media ID whose file to delete.
    :param file_id: str|int - The file ID to delete.
    :return: bool - True if the file was deleted successfully, False otherwise.
    """
    file_endpoint = f"{ctx["config"]["host"]}/entity/file/{file_id}?_format=json"
    file_response = issue_request(ctx["config"], "DELETE", file_endpoint)
    if file_response.status_code == 204:
        ctx["log"].info(
            f"File {file_id} (from media {media_id}) deleted."
        )
        return True
    else:
        ctx["log"].error(
            f"File {file_id} (from media {media_id}) not deleted (HTTP response code {file_response.status_code})."
        )
        return False

def find_file_id_in_media(media_json: dict) -> Union[None, int]:
    """Find the file ID in a media JSON response.
    Parameters
    :param media_json: dict - The JSON body of the media entity.
    :return: int|None - The file ID if found, or None if not found.
    """
    for file_field in FILE_FIELDS:
        if file_field in media_json:
            file_info = media_json[file_field]
            if len(file_info) > 0:
                return file_info[0].get("target_id")
    return None


def get_media_field_values(config: dict, media_id: Union[int, str]) -> dict:
    """Get a media's field data so we can use it during PATCH updates, which replace a field's values."""
    if config["standalone_media_url"] is True:
        media_url = config["host"] + "/media/" + media_id + "?_format=json"
    else:
        media_url = config["host"] + "/media/" + media_id + "/edit?_format=json"

    get_media_response = issue_request(config, "GET", media_url)
    media_fields = get_media_response.json()
    return media_fields
