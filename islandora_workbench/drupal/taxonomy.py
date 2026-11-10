"""Higher level taxonomy related functions for creating and updating terms in Drupal."""

import logging
import re
import sys
from typing import Union

from islandora_workbench.drupal.client import find_term_in_vocab, send_to_term_endpoint, get_term_id_from_uri, \
    issue_request
from islandora_workbench.drupal.fields import get_field_definitions, WorkbenchFieldFactory
from islandora_workbench.utils import value_is_numeric


def find_or_create_term(
    config: dict, vocab_id: str, term_name: str, term_csv_row: Union[dict, None] = None
) -> Union[str, bool]:
    """Adds a term to the target vocabulary. Returns the new term's ID
    if successful (or if the term already exists) or False if not.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        vocab_id: string
            The vocabulary ID.
        term_name: string
            The term name from CSV.
        term_csv_row: dict|None
            ECSV row containing term field data. Only present if we are creating
            complex or child terms, not present for simple terms.
        Returns
        -------
        string|boolean
            The term ID, or False term was not created.
    """
    # Check to see if term exists; if so, return its ID, if not, proceed to create it.
    tid = find_term_in_vocab(config, vocab_id, term_name)
    if value_is_numeric(tid):
        if (config["task"] == "create" or config["task"] == "update") and config[
            "log_term_creation"
        ] is True:
            logging.info(
                'Term "%s" (term ID %s) already exists in vocabulary "%s".',
                term_name,
                tid,
                vocab_id,
            )
        if config["task"] == "create_terms":
            logging.info(
                'Term "%s" (term ID %s) already exists in vocabulary "%s".',
                term_name,
                tid,
                vocab_id,
            )
        return tid

    if vocab_id in config["protected_vocabularies"]:
        logging.warning(
            f'Term "{term_name}" is not in its designated vocabulary ({vocab_id}) and will not be added since the vocabulary is registered in the "protected_vocabularies" config setting.'
        )
        return False

    if config["allow_adding_terms"] is False:
        logging.warning(
            f'Term "{term_name}" does not exist in the vocabulary "{vocab_id}". To create new taxonomy terms, you must add "allow_adding_terms: true" to your configuration file.'
        )
        return False

    if len(term_name) > 255:
        truncated_term_name = term_name[:255]
        message = (
            'Term "'
            + term_name
            + '"'
            + "provided in the CSV data exceeds Drupal's maximum length of 255 characters."
        )
        message_2 = ' It has been trucated to "' + truncated_term_name + '".'
        logging.info(message + message_2)
        term_name = truncated_term_name

    term_field_data = get_term_field_data(config, vocab_id, term_name, term_csv_row)
    if term_field_data is False:
        # @todo: Failure details should be logged in get_term_field_data().
        logging.warning(
            'Unable to create term "'
            + term_name
            + '" because Workbench could not get term field data.'
        )
        return False

    # Common values for all terms, simple and complex.
    term = {
        "vid": [{"target_id": str(vocab_id), "target_type": "taxonomy_vocabulary"}],
        "name": [{"value": term_name}],
    }

    term.update(term_field_data)
    return send_to_term_endpoint(config, term)


def prepare_term_id(
    config: dict, vocab_ids: Union[list, bool], field_name: str, term: str
) -> Union[str, None]:
    """Checks to see if 'term' is numeric (i.e., a term ID) and if it is, returns it as
    is. If it's not (i.e., it's a string term name) it looks for the term name in the
    referenced vocabulary and returns its term ID if the term exists, and if it doesn't
    exist, creates the term and returns the new term ID.
    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    vocab_ids: list|boolean
        The vocabulary IDs associated with the field handling code calling this function.
    field_name: string
        The field's machine name.
    term: string
        The term name from CSV.
    Returns
    -------
    int|None
        The term ID, existing or newly created. None if there are no vocabularies
        associated with the field identified in field_name (which is the case with
        "Filter by an entity reference view" reference type fields)or if the vocab
        ID is otherwise unknown.
    """
    term = str(term)
    term = term.strip()
    if value_is_numeric(term) and field_name not in config["columns_with_term_names"]:
        return term
    entity_reference_view_endpoints = config["entity_reference_view_endpoints"] if "entity_reference_view_endpoints" in config else {}
    if not entity_reference_view_endpoints and vocab_ids is False:
        return None
    # Special case: if the term starts with 'http', assume it's a Linked Data URI
    # and get its term ID from the URI.
    elif term.startswith("http"):
        # Note: get_term_id_from_uri() will return False if the URI doesn't match a term.
        tid_from_uri = get_term_id_from_uri(config, term)
        if value_is_numeric(tid_from_uri):
            return str(tid_from_uri)
    else:
        if entity_reference_view_endpoints.get(field_name, False):
            headers = {"Content-Type": "application/json"}
            endpoint = entity_reference_view_endpoints.get(field_name, False)
            if ":" in term:
                [tentative_vocab_id, term_name] = term.split(":", maxsplit=1)
                tentative_vocab_id = tentative_vocab_id.strip()
                term_name = term_name.strip()
                response = issue_request(
                    config,
                    "GET",
                    endpoint + "?name=" + term_name + "&vid=" + tentative_vocab_id,
                    headers,
                )
                if response.status_code == 200:
                    term_response_body = response.json()
                    if term_response_body:
                        return term_response_body[0]["tid"][0]["value"]
                    tid = find_or_create_term(config, tentative_vocab_id, term_name)
                    return tid
                else:
                    logging.warning(
                        "Term '%s' not found, HTTP response code was %s.",
                        term,
                        response.status_code,
                    )
                    return None
            else:
                response = issue_request(
                    config, "GET", endpoint + "?name=" + term, headers
                )
                if response.status_code == 200:
                    term_response_body = response.json()
                    if term_response_body:
                        return term_response_body[0]["tid"][0]["value"]
                    logging.warning(
                        "Term '%s' not found. Cannot create it using entity_reference_view_endpoints without a provided vocabulary.",
                        term,
                    )
                    return None
                else:
                    logging.warning(
                        "Term '%s' not found, HTTP response code was %s.",
                        term,
                        response.status_code,
                    )
                    return None
        elif len(vocab_ids) == 1:
            # A namespace is not needed but it might be present. If there is,
            # since this vocabulary is the only one linked to its field,
            # we remove it before sending it to create_term().
            namespaced = re.search(":", term)
            if namespaced:
                [vocab_id, term_name] = term.split(":", maxsplit=1)
                if vocab_id == vocab_ids[0]:
                    tid = find_or_create_term(config, vocab_id.strip(), term_name.strip())
                    return tid
                else:
                    tid = find_or_create_term(config, vocab_ids[0].strip(), term.strip())
                    return tid
            else:
                tid = find_or_create_term(config, vocab_ids[0].strip(), term.strip())
                return tid
        else:
            # Term names used in multi-taxonomy fields. They need to be namespaced with
            # the taxonomy ID.
            #
            # If the field has more than one vocabulary linked to it, we don't know which
            # vocabulary the user wants a new term to be added to, and if the term name is
            # already used in any of the taxonomies linked to this field, we also don't know
            # which vocabulary to look for it in to get its term ID. Therefore, we always need
            # to namespace term names if they are used in multi-taxonomy fields.
            #
            # Split the namespace/vocab ID from the term name on ':'.
            if ":" in term:
                [tentative_vocab_id, term_name] = term.split(":", maxsplit=1)
                for vocab_id in vocab_ids:
                    if tentative_vocab_id == vocab_id:
                        tid = find_or_create_term(config, vocab_id.strip(), term_name.strip())
                        return tid
            else:
                if isinstance(vocab_ids, str):
                    tid = find_or_create_term(config, vocab_ids.strip(), term.strip())
                    return tid

                message = f"Because The field '{field_name}' allows more than one vocabulary the term '{term}' must be namespaced."
                message = (
                    message
                    + "See documentation at https://mjordan.github.io/islandora_workbench_docs/fields/#using-term-names-in-multi-vocabulary-fields"
                )
                logging.error(message)
                sys.exit("Error: " + message)

    # Explicitly return None if hasn't returned from one of the conditions above, e.g. if
    # the term name contains a colon and it wasn't namespaced with a valid vocabulary ID.
    return None


def get_term_field_data(
    config: dict, vocab_id: str, term_name: str, term_csv_row: Union[dict, None]
) -> Union[dict, bool]:
    """Assemble the dict that will be added to the 'term' dict in create_term(). status, description,
    weight, parent, default_langcode, path fields are added here, even for simple term_name-only
    terms. Check the vocabulary CSV file to see if there is a corresponding row. If the vocabulary
    has any required fields, and any of them are absent, return False.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        vocab_id: string
            The vocabulary ID.
        term_name: string
            The term name from CSV. (Not used in this function)
        term_csv_row: Union[dict, None]
            ECSV row containing term field data.
        Returns
        -------
        dict|boolean
            The dict containing the term field data, or False if this is not possible.
            @note: reason why creating JSON is not possible should be logged in this function.
    """
    if (
        term_csv_row is not None
        and "published" in term_csv_row.keys()
        and len(term_csv_row["published"]) > 0
    ):
        published_status = term_csv_row["published"]
    else:
        published_status = True

    # 'vid' and 'name' are added in create_term().
    term_field_data = {
        "status": [{"value": published_status}],
        "description": [{"value": "", "format": None}],
        "weight": [{"value": 0}],
        "parent": [{"target_type": "taxonomy_term", "target_id": None}],
        "default_langcode": [{"value": True}],
        "path": [{"alias": None, "pid": None, "langcode": "en"}],
    }

    # We're creating a simple term, with only a term name.
    if term_csv_row is None:
        return term_field_data
    # We're creating a complex term, with extra fields.
    else:
        # Importing the workbench_fields module at the top of this module with the
        # rest of the imports causes a circular import exception, so we do it here.
        vocab_field_definitions = get_field_definitions(
            config, "taxonomy_term", vocab_id.strip()
        )

        # Build the JSON from the CSV row and create the term.
        vocab_csv_column_headers = term_csv_row.keys()
        for field_name in vocab_csv_column_headers:
            # term_name is the "id" field in the vocabulary CSV and not a field in the term JSON, so skip it.
            if field_name == "term_name":
                continue

            # "published" is a reserved column name in the vocabulary CSV and not a field in the term JSON, so skip it.
            if field_name == "published":
                continue

            # 'parent' field is present and not empty, so we need to look up the parent term. All terms
            # that are parents will have already been created back in workbench.create_terms() as long as
            # they preceded the children. If they come after the children in the CSV, we create the child
            # term anyway but log that the parent could not be found.
            if "parent" in term_csv_row and len(term_csv_row["parent"].strip()) != 0:
                parent_tid = find_term_in_vocab(
                    config, vocab_id, term_csv_row["parent"]
                )
                if value_is_numeric(parent_tid):
                    term_field_data["parent"][0]["target_id"] = str(parent_tid)
                else:
                    # Create the term, but log that its parent could not be found.
                    message = (
                        'Term "'
                        + term_csv_row["term_name"]
                        + '" added to vocabulary "'
                        + vocab_id
                        + '", but without its parent "'
                        + term_csv_row["parent"]
                        + "\", which isn't present in that vocabulary (possibly hasn't been create yet?)."
                    )
                    logging.warning(message)

            # 'parent' is not a field added to the term JSON in the field handlers, so skip it.
            if field_name == "parent":
                continue

            # Set 'description' and 'weight' JSON values if there are corresponding columns in the CSV.
            if "weight" in term_csv_row and len(term_csv_row["weight"].strip()) != 0:
                if value_is_numeric(term_csv_row["weight"]):
                    term_field_data["weight"][0]["value"] = str(term_csv_row["weight"])
                else:
                    # Create the term, but log that its weight could not be populated.
                    message = (
                        'Term "'
                        + term_csv_row["term_name"]
                        + '" added to vocabulary "'
                        + vocab_id
                        + '", but without its weight "'
                        + term_csv_row["weight"]
                        + '", which must be an integer.'
                    )
                    logging.warning(message)

            # 'weight' is not a field added to the term JSON in the field handlers, so skip it.
            if field_name == "weight":
                continue

            if (
                "description" in term_csv_row
                and len(term_csv_row["description"].strip()) != 0
            ):
                term_field_data["description"][0]["value"] = term_csv_row["description"]

            # 'description' is not a field added to the term JSON in the field handlers, so skip it.
            if field_name == "description":
                continue

            # Assemble Drupal field structures from CSV data. If new field types are added to
            # fields.py, they need to be registered in the following if/elif/else block.

            field = WorkbenchFieldFactory.get_field_handler(
                vocab_field_definitions[field_name]["field_type"]
            )
            term_field_data = field.create(
                config,
                vocab_field_definitions,
                term_field_data,
                term_csv_row,
                field_name,
            )

        return term_field_data
