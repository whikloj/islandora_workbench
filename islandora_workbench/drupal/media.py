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