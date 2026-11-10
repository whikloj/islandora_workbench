import copy
import datetime
import logging
import os
import re
import sys
from typing import Union, Any, Generator

import edtf_validate.valid_edtf

from islandora_workbench.data_io.csv.csv_reader import CsvReader
from islandora_workbench.exceptions import WorkbenchValidationException

from islandora_workbench.utils.string_utils import service_file_present, value_is_numeric
from islandora_workbench.drupal.client import ping_url_alias, get_term_id_from_uri, get_term_vocab, find_term_in_vocab, \
    ping_uid, ping_view_endpoint
from islandora_workbench.drupal.utils import validate_language_code



def validate_geolocation_fields_action(
    config: dict, row: dict[Union[str, Any], Union[str, Any]], field_name: str
) -> None:
    """Check that values in the given CSV row's geolocation field are valid lat,long pairs.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param row: dict - The CSV row.
    :param field_name: string - The geolocation field's machine name.

    :raise WorkbenchValidationException: If the CSV row contains an invalid geolocation field value.
    """
    if field_name in row:
        delimited_field_values = row[field_name].split(config["subdelimiter"])
        for field_value in delimited_field_values:
            if len(field_value.strip()):
                if not validate_latlong_value(field_value.strip()):
                    message = (
                        f'Value in field "{field_name}" in row with ID {row[config["id_field"]]} ({field_value}) is not a valid lat,long pair.'
                    )
                    raise WorkbenchValidationException(message)


def validate_latlong_value(latlong: str) -> bool:
    """Validate that the value in 'latlong' is a valid latitude,longitude pair.
    Parameters
    :param latlong: string - The lat,long string.
    :return: boolean - True if it is valid, False if not.
    """
    # Remove leading \ that may be present if input CSV is from a spreadsheet.
    latlong = latlong.lstrip("\\")
    if re.match(
        r"^[-+]?([1-8]?\d(\.\d+)?|90(\.0+)?),\s*[-+]?(180(\.0+)?|((1[0-7]\d)|([1-9]?\d))(\.\d+)?)$",
        latlong,
    ):
        return True
    else:
        return False

def validate_link_fields_action(config: dict, row: dict[Union[str, Any], Union[str, Any]], field_name: str) -> None:
    """Check that values in the given CSV row's link field are valid URLs.

    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param row: dict - The CSV row.
    :param field_name: string - The link field's machine name.

    :raise WorkbenchValidationException: If the CSV row contains an invalid link field value.
    """
    if field_name in row:
        delimited_field_values = row[field_name].split(config["subdelimiter"])
        for field_value in delimited_field_values:
            if len(field_value.strip()):
                if not validate_link_value(field_value.strip()):
                    message = (
                        f'Value in field "{field_name}" in row with ID {row[config["id_field"]]} ({field_value}) is not a valid link.'
                    )
                    raise WorkbenchValidationException(message)

def validate_link_value(link_value: str) -> bool:
    """Validates that the value in 'link_value' starts with either 'http://' or 'https://'
    and optionally contains the url/label delimiter '%%'.
    Parameters
        ----------
        link_value : string
            The URL.
        Returns
        -------
        boolean
            True if it does, False if not.
    """
    parts = link_value.split("%%", 1)
    if re.match(r"^https?://", parts[0]):
        return True
    else:
        return False

def validate_authority_link_value_action(config: dict, authority_sources: list, row: dict, field_name: str) -> None:
    """Check that values in the given CSV row's authority link field are valid authority link values.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param authority_sources: list - The list of authority sources (e.g. lcsh, cash, viaf, etc.) configured for the field.
    :param row: dict - The CSV row.
    :param field_name: string - The authority link field's machine name.

    :raises WorkbenchValidationException: If the CSV row contains an invalid authority link field value.
    """
    if field_name in row:
        delimited_field_values = row[field_name].split(config["subdelimiter"])
        for field_value in delimited_field_values:
            if len(field_value.strip()):
                if not validate_authority_link_value(field_value.strip(), authority_sources):
                    message = ('Value in field "' + field_name + '" in row with ID "' + row[
                        config["id_field"]] + '" (' + field_value + ") is not a valid authority link field value.")
                    raise WorkbenchValidationException(message)

def validate_authority_link_value(
    authority_link_value: str, authority_sources: list
) -> bool:
    """Validates that the value in 'authority_link_value' has a 'source' and that the URI
    component starts with either 'http://' or 'https://'.
    Parameters
        ----------
        authority_link_value : string
            The authority link string, with a sourcea, URI, and optionally a title.
        authority_sources : list
            The list of authority sources (e.g. lcsh, cash, viaf, etc.) configured for the field.
        Returns
        -------
        boolean
            True if it does, False if not.
    """
    parts = authority_link_value.split("%%", 2)
    if parts[0] not in authority_sources:
        return False
    if re.match(r"^https?://", parts[1]):
        return True
    else:
        return False


def validate_node_created_date_action(
    config: dict, row: dict[Union[str, Any], Union[str, Any]], field_name: str = "created"
) -> None:
    """Validate that the value in the "created" field of the given CSV row is in the format used by Drupal's 'created' node property
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param row: dict - The CSV row.
    :param field_name: string - The field name to validate.
    :raises WorkbenchValidationException: If the CSV row contains a date in the wrong format.
    """
    if field_name in row:
        field_value = row[field_name]
        if len(field_value) > 0:
            # matches = re.match(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$', field_value)
            if not validate_node_created_date_string(field_value):
                message = (
                    'CSV field "created" in record with ID '
                    + row[config["id_field"]]
                    + ' contains a date "'
                    + field_value
                    + '" that is not formatted properly.'
                )
                raise WorkbenchValidationException(message)

            now = datetime.datetime.now()
            # Remove the GMT differential at the end of the time string.
            date_string_trimmed = re.sub(r"[+-]\d\d:\d\d$", "", field_value)
            created_date = datetime.datetime.strptime(
                date_string_trimmed, "%Y-%m-%dT%H:%M:%S"
            )
            if created_date > now:
                message = (
                    'CSV field "created" in record with ID '
                    + row[config["id_field"]]
                    + ' contains a date "'
                    + field_value
                    + '" that is in the future.'
                )
                raise WorkbenchValidationException(message)


def validate_node_created_date_string(created_date_string: str) -> bool:
    """Checks that date_string is in the format used by Drupal's 'created' node property,
    e.g., 2020-11-15T23:49:22+00:00.
    Parameters
    :param created_date_string: str - The date string.
    :return: bool - True if it is valid, False if not.
    """
    if re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$", created_date_string):
        return True
    else:
        return False


def validate_weight_value(weight_value: str) -> bool:
    """Validates that the value in 'weight_value' is a numeric value.
    Parameters
    :param weight_value: string - The weight value.
    :return: boolean - True if it is valid, False if not
    """
    weight = weight_value.lstrip("0")
    if re.match(r"^\d+$", weight) and int(weight) > 0:
        return True
    else:
        return False


def validate_url_aliases_action(
    config: dict, row: dict[Union[str, Any], Union[str, Any]]
) -> None:
    """Checks that URL aliases in the given CSV row don't already exist.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param row: dict - The CSV row.
    :raise WorkbenchValidationException: If the CSV row contains a URL alias that already exists in the target Drupal.
    """
    if "url_alias" in row and len(row["url_alias"]) > 0:
        field_value = row["url_alias"]
        if field_value.strip()[0] != "/":
            message = (
                'CSV field "url_alias" in record with ID '
                + row[config["id_field"]]
                + ' contains an alias "'
                + field_value
                + '" that is missing its leading /.'
            )
            raise WorkbenchValidationException(message)

        alias_ping = ping_url_alias(config, field_value)
        # @todo: Add 301 and 302 as acceptable status codes?
        if alias_ping == 200:
            message = (
                'CSV field "url_alias" in record with ID '
                + row[config["id_field"]]
                + ' contains an alias "'
                + field_value
                + '" that already exists.'
            )
            raise WorkbenchValidationException(message)


def validate_node_uid_action(
    config: dict, row: dict[Union[str, Any], Union[str, Any]]
) -> None:
    """Checks that the user identified in the 'uid' field of the given CSV row exists
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param row: dict - The CSV row.

    :raise WorkbenchValidationException: If the user identified in the 'uid' field does not exist in the target Drupal.
    """
    if "uid" in row.values():
        field_value = row["uid"]
        if len(field_value) > 0 and ping_uid(config, field_value) is False:
            message = (
                f'CSV field "uid" in record with ID {row[config["id_field"]]} contains a user ID "{field_value}" that does not exist in the target Drupal.'
            )
            raise WorkbenchValidationException(message)


def validate_typed_relation_field_value_action(
    config: dict,
    field_definitions: dict,
    csv_field_name: str,
    csv_field_value: str,
    row_id: int,
) -> bool:
    """Validate that the value(s) in 'csv_field_value' are valid for a typed relation field.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_field_name: str - The name of the CSV field to validate.
    :param csv_field_value: str - The value from the CSV field to validate.
    :param row_id: int - The ID of the current row.
    :return: bool - Returns True if new term names are present and valid to add; otherwise False.
    :raise WorkbenchValidationException: If the CSV row contains an invalid typed relation field value.
    """
    new_terms_present = False
    vocabularies = False
    if "vocabularies" in field_definitions[csv_field_name]:
        vocabularies = get_field_vocabularies(config, field_definitions, csv_field_name)
    delimited_field_values = csv_field_value.split(config["subdelimiter"])
    for field_value in delimited_field_values:
        if len(field_value) == 0:
            continue
        if not re.match("^[0-9a-zA-Z]+:[0-9a-zA-Z]+:.+$", field_value.strip()):
            message = (
                'Value in field "'
                + csv_field_name
                + '" in row with ID '
                + row_id
                + " ("
                + field_value
                + ") does not use the structure required for typed relation fields."
            )
            raise WorkbenchValidationException(message)

        # Then, check to see if the relator string (the first two parts of the
        # value) exist in the field_definitions[fieldname]['typed_relations'] list.
        typed_relation_value_parts = csv_field_value.split(":", 2)
        relator_string = (
            typed_relation_value_parts[0] + ":" + typed_relation_value_parts[1]
        )
        if relator_string not in field_definitions[csv_field_name]["typed_relations"]:
            message = (
                'Value in field "'
                + csv_field_name
                + '" in row with ID '
                + row_id
                + " contains a relator ("
                + relator_string
                + ") that is not configured for that field."
            )
            raise WorkbenchValidationException(message)
        try:
            if len(vocabularies) > 0:
                delimited_field_values_without_relator_strings = []
                # Strip the relator string out from field_value, leaving the vocabulary ID and term ID/name/URI.
                term_to_check = re.sub("^[0-9a-zA-Z]+:[0-9a-zA-Z]+:", "", field_value)
                delimited_field_values_without_relator_strings.append(term_to_check)

                field_value_to_check = config["subdelimiter"].join(
                    delimited_field_values_without_relator_strings
                )
                if validate_taxonomy_reference_value_action(
                    config,
                    field_definitions,
                    csv_field_name,
                    field_value_to_check,
                    row_id,
                ):
                    new_terms_present = True
        except TypeError:
            message = f'Workbench cannot get vocabularies linked to field "{csv_field_name}". Please confirm that field has at least one vocabulary.'
            raise WorkbenchValidationException(message)
    return new_terms_present

def validate_numeric_field_action(config: dict, row: dict, field_name: str, is_decimal: bool) -> None:
    """Validate that the value(s) in 'field_name' are valid numeric fields.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param row: dict - The row from the CSV file.
    :param field_name: str - The name of the CSV field to validate.
    :param is_decimal: bool - Whether or not the field is a decimal number.

    :raise WorkbenchValidationException: If the field is not a valid numeric field.
    """
    delimited_field_values = row[field_name].split(config["subdelimiter"])
    for field_value in delimited_field_values:
        if len(field_value.strip()):
            if not value_is_numeric(field_value.strip(), allow_decimals=is_decimal):
                message = ('Value in field "' + field_name + '" in row with ID ' + row[
                    config["id_field"]] + " (" + field_value + ") is not a valid " + "decimal" if is_decimal else "integer" + " value.")
                raise WorkbenchValidationException(message)


def validate_taxonomy_reference_value_action(
    config: dict,
    field_definitions: dict,
    csv_field_name: str,
    csv_field_value: str,
    row_id: Union[int, str],
) -> bool:
    """Validate that the value(s) in 'csv_field_value' exist in the vocabularies or are valid to add.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_field_name: str - The name of the CSV field to validate.
    :param csv_field_value: str - The value from the CSV field to validate.
    :param row_id: Union[int, str] - The ID of the current row.
    :return: bool - Returns True if new term names are present and valid to add; otherwise False.
    """

    # Not an entity reference field on a vocabulary or a typed relation field.
    if (
        field_definitions[csv_field_name]["field_type"]
        in ["entity_reference", "typed_relation"]
        and "vocabularies" not in field_definitions[csv_field_name]
    ):
        return False

    this_fields_vocabularies = get_field_vocabularies(
        config, field_definitions, csv_field_name
    )

    this_field_vocabularies_string = ",".join(this_fields_vocabularies)

    terms_to_check = csv_field_value.split(config["subdelimiter"])
    terms_with_errors = []
    new_terms_to_add = []
    for field_value in terms_to_check:
        # Try to catch internal WorkbenchValidationExceptions and accumulate them in a list
        try:
            # If this is a multi-taxonomy field, all term names (not IDs or URIs) must be namespaced using the vocab_id:term_name pattern,
            # regardless of whether config['allow_adding_terms'] is True. Also, we need to accommodate terms that are namespaced
            # and also contain a ':'.
            if (
                len(this_fields_vocabularies) > 1
                and value_is_numeric(field_value) is False
                and not field_value.startswith("http")
            ):
                field_value = field_value.strip()
                if ":" in field_value:
                    # If the : is present, validate that the namespace is one of the vocabulary IDs referenced by this field.
                    tentative_namespace, _ = field_value.split(":", 1)
                    if tentative_namespace not in this_fields_vocabularies:
                        message = (
                            f'Vocabulary ID "{tentative_namespace}" used in CSV column "{csv_field_name}", row with ID {str(row_id)}'
                            + " does not match any of the vocabularies referenced by the corresponding Drupal field ("
                            + this_field_vocabularies_string
                            + ")."
                        )
                        raise WorkbenchValidationException(message)
                else:
                    message = (
                        f'Term names in CSV field "{csv_field_name}" require a vocabulary namespace; CSV value '
                        f'"{field_value}" in row with ID {str(row_id)} does not have one.'
                    )
                    raise WorkbenchValidationException(message)

                if len(field_value.strip()) > 255:
                    message = (
                        f'CSV field "{csv_field_name}" in record with ID {str(row_id)} contains a taxonomy term that exceeds Drupal\'s limit of 255 characters (length of term is '
                        + str(len(field_value))
                        + " characters)."
                    )
                    raise WorkbenchValidationException(message)

            # Check to see if field_value is a member of the field's vocabularies. First, check whether field_value is a term ID.
            if (
                value_is_numeric(field_value)
                and csv_field_name not in config["columns_with_term_names"]
            ):
                field_value = field_value.strip()
                term_in_vocabs = False
                for vocab_id in this_fields_vocabularies:
                    term_vocab = get_term_vocab(config, field_value)
                    if term_vocab == vocab_id:
                        term_in_vocabs = True
                        break
                if not term_in_vocabs:
                    message = (
                        f'CSV field "{csv_field_name}" in row with ID {str(row_id)} contains a term ID ('
                        + f"{field_value}) that is "
                    )
                    if len(this_fields_vocabularies) > 1:
                        message += f"not in one of the referenced vocabularies ({this_field_vocabularies_string})."
                    else:
                        message += f'not in the referenced vocabulary ("{this_fields_vocabularies[0]}").'
                    raise WorkbenchValidationException(message)
            # Then check values that are URIs.
            elif field_value.startswith("http"):
                tid_from_uri = get_term_id_from_uri(config, field_value)
                if value_is_numeric(tid_from_uri):
                    term_vocab = get_term_vocab(config, tid_from_uri)
                    term_in_vocabs = (
                        term_vocab != False and term_vocab in this_fields_vocabularies
                    )
                    if not term_in_vocabs:
                        message = (
                            f'CSV field "{csv_field_name}" in row with ID {str(row_id)} contains a term URI ({field_value}) that is '
                            + "not in "
                            + ("one of " if len(this_fields_vocabularies) > 1 else "")
                            + (
                                "the referenced vocabular"
                                + ("ies" if len(this_fields_vocabularies) > 1 else "y")
                                + f"({this_field_vocabularies_string})."
                            )
                        )
                        raise WorkbenchValidationException(message)
                else:
                    message = f'Term URI "{field_value}" used in CSV column "{csv_field_name}" row with ID {str(row_id)} does not match any terms.'
                    raise WorkbenchValidationException(message)
            # Finally, check values that are string term names.
            else:

                for vocabulary in this_fields_vocabularies:
                    tid = find_term_in_vocab(config, vocabulary, field_value)
                    if not value_is_numeric(tid):
                        # Single taxonomy fields.
                        if len(this_fields_vocabularies) == 1:
                            if config["allow_adding_terms"] is True:
                                # Warn if namespaced term name is not in specified vocab.
                                if tid is not False:
                                    continue
                                if field_value not in new_terms_to_add:
                                    new_terms_to_add.append(field_value)
                                if len(field_value.strip()) > 255:
                                    message = (
                                        f'CSV field "{csv_field_name}" in record with ID {str(row_id)} contains a taxonomy term that exceeds Drupal\'s limit of 255 characters (length of term is '
                                        + str(len(field_value))
                                        + " characters)."
                                    )
                                    raise WorkbenchValidationException(message)
                                message = (
                                    f'CSV field "{csv_field_name}" in row with ID {str(row_id)} contains a term ("{field_value}")'
                                    + '") that is '
                                )

                                if (
                                    this_fields_vocabularies[0]
                                    in config["protected_vocabularies"]
                                ):
                                    message += (
                                        'not in the referenced vocabulary ("'
                                        + this_fields_vocabularies[0]
                                        + '"). The term will not be created since "'
                                        + this_fields_vocabularies[0]
                                        + '" is registered in the "protected_vocabularies" config setting.'
                                    )
                                else:
                                    message += (
                                        'not in the referenced vocabulary ("'
                                        + this_fields_vocabularies[0]
                                        + '"). That term will be created.'
                                    )
                                if config["validate_terms_exist"] is True:
                                    logging.warning(message)
                            else:
                                message = (
                                    f'CSV field "{csv_field_name}" in row with ID {str(row_id)} contains a term ("{field_value}")'
                                    + '") that is '
                                    + f'not in the referenced vocabulary ("{this_fields_vocabularies[0]}").'
                                )
                                raise WorkbenchValidationException(message)

                        # If this is a multi-taxonomy field, all term names must be namespaced using the vocab_id:term_name pattern,
                        # regardless of whether config['allow_adding_terms'] is True.
                        elif len(this_fields_vocabularies) > 1:
                            # Check to see if the namespaced vocab is referenced by this field.
                            namespace_vocab_id, namespaced_term_name = (
                                field_value.split(":", 1)
                            )
                            if namespace_vocab_id not in this_fields_vocabularies:
                                message = (
                                    f'CSV field "{csv_field_name}" in row with ID {str(row_id)} contains a namespaced term name '
                                    f'("{namespaced_term_name.strip()}") that specifies a vocabulary not associated with that field ('
                                    + namespace_vocab_id
                                    + ")."
                                )
                                raise WorkbenchValidationException(message)

                            tid = find_term_in_vocab(
                                config, namespace_vocab_id, namespaced_term_name
                            )

                            # Warn if namespaced term name is not in specified vocab.
                            if config["allow_adding_terms"] is True:
                                if tid is False and field_value not in new_terms_to_add:
                                    message = (
                                        f'CSV field "{csv_field_name}" in row with ID {str(row_id)} contains a term ("'
                                        + namespaced_term_name.strip()
                                        + '") that is '
                                    )

                                    if (
                                        namespace_vocab_id
                                        in config["protected_vocabularies"]
                                    ):
                                        message += (
                                            f'not in the referenced vocabulary ("{namespace_vocab_id}"). The term will not be created since "'
                                            + namespace_vocab_id
                                            + '" is registered in the "protected_vocabularies" config setting.'
                                        )
                                    else:
                                        message += f'not in the referenced vocabulary ("{namespace_vocab_id}"). That term will be created.'
                                    if config["validate_terms_exist"] is True:
                                        logging.warning(message)
                                    new_terms_to_add.append(field_value)
                                    if len(field_value.strip()) > 255:
                                        message = (
                                            f'CSV field "{csv_field_name}" in record with ID {str(row_id)} contains a taxonomy term that exceeds Drupal\'s limit of 255 characters (length of term is '
                                            + str(len(field_value))
                                            + " characters)."
                                        )
                                        raise WorkbenchValidationException(message)
                            # Die if namespaced term name is not specified vocab.
                            elif tid is False:
                                message = (
                                    f'CSV field "{csv_field_name}" in row with ID {str(row_id)} contains a term ("'
                                    + namespaced_term_name.strip()
                                    + '") that is not in the referenced vocabulary ("'
                                    + namespace_vocab_id
                                    + '").'
                                )
                                raise WorkbenchValidationException(message)
        except WorkbenchValidationException as e:
            terms_with_errors.append((row_id, str(e)))
    if len(terms_with_errors) > 0:
        error_messages = "\n".join(
            [
                f"Row ID {row_id}: {error_message}"
                for row_id, error_message in terms_with_errors
            ]
        )
        raise WorkbenchValidationException(
            f"One or more taxonomy reference values are invalid:\n{error_messages}",
            wrapped=True,
        )
    return len(new_terms_to_add) > 0


def validate_geolocation_fields(
    config: dict,
    field_definitions: dict,
    csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None],
) -> None:
    """Validate lat,long values in fields that are of type 'geolocation'.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: Generator - The CSV data.
    """
    geolocation_fields_present = False
    for count, row in enumerate(csv_data, start=1):
        for field_name in field_definitions.keys():
            if field_definitions[field_name]["field_type"] == "geolocation":
                if field_name in row:
                    geolocation_fields_present = True
                    validate_geolocation_fields_action(config, row, field_name)

    if geolocation_fields_present is True:
        message = "OK, geolocation field values in the CSV file validate."
        print(message)
        logging.info(message)


def validate_link_fields(
    config: dict,
    field_definitions: dict,
    csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None],
) -> None:
    """Validate values in fields that are of type 'link'.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: Generator - The CSV data.
    """
    link_fields_present = False
    for count, row in enumerate(csv_data, start=1):
        for field_name in field_definitions.keys():
            if field_definitions[field_name]["field_type"] == "link":
                if field_name in row:
                    link_fields_present = True
                    delimited_field_values = row[field_name].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()):
                            if not validate_link_value(field_value.strip()):
                                message = (
                                    'Value in field "'
                                    + field_name
                                    + '" in row with ID '
                                    + row[config["id_field"]]
                                    + " ("
                                    + field_value
                                    + ") is not a valid link field value."
                                )
                                logging.error(message)
                                sys.exit("Error: " + message)

    if link_fields_present is True:
        message = "OK, link field values in the CSV file validate."
        print(message)
        logging.info(message)


def validate_authority_link_fields(
    config: dict,
    field_definitions: dict,
    csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None],
) -> None:
    """Validate values in fields that are of type 'authority_link'.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: Generator - The CSV data.
    """
    if config["task"] == "create_terms":
        config["id_field"] = "term_name"

    authority_link_fields_present = False
    for count, row in enumerate(csv_data, start=1):
        for field_name in field_definitions.keys():
            if field_definitions[field_name]["field_type"] == "authority_link":
                if field_name in row:
                    authority_link_fields_present = True
                    delimited_field_values = row[field_name].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()):
                            if not validate_authority_link_value(
                                field_value.strip(),
                                field_definitions[field_name]["authority_sources"],
                            ):
                                message = (
                                    'Value in field "'
                                    + field_name
                                    + '" in row with ID "'
                                    + row[config["id_field"]]
                                    + '" ('
                                    + field_value
                                    + ") is not a valid authority link field value."
                                )
                                logging.error(message)
                                sys.exit("Error: " + message)

    if authority_link_fields_present is True:
        message = "OK, authority link field values in the CSV file validate."
        print(message)
        logging.info(message)


def validate_media_track_fields(config: dict, csv_data: CsvReader) -> None:
    """Validate values in fields that are of type 'media_track'.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param csv_data: WorkbenchCsvReader - The CSV data.
    """
    media_track_fields_present = False
    # Must accommodate multiple media track fields in the same CSV (e.g. audio and video media in the
    # same CSV, each with its own track column). Therefore, we'll need to get the field definitions
    # for more than one media bundle.
    media_track_field_definitions = dict()
    csv_column_headers = copy.copy(csv_data.get_field_names())
    for column_header in csv_column_headers:
        if column_header.startswith("media:"):
            # Assumes well-formed column headers.
            media_bundle_name_parts = column_header.split(":")
            media_bundle_name = media_bundle_name_parts[1]
            if media_bundle_name not in config["media_track_file_fields"]:
                message = (
                    'Media type "'
                    + media_bundle_name
                    + '" in the CSV column header "'
                    + column_header
                    + '" is not registered in the "media_track_file_fields" configuration setting.'
                )
                logging.error(message)
                sys.exit("Error: " + message)

            media_track_field_definitions[media_bundle_name] = get_field_definitions(
                config, "media", media_bundle_name
            )
            for count, row in enumerate(csv_data.get_csv_data(), start=1):
                for field_name in media_track_field_definitions[
                    media_bundle_name
                ].keys():
                    if (
                        media_track_field_definitions[media_bundle_name][field_name][
                            "field_type"
                        ]
                        == "media_track"
                    ):
                        fully_qualified_field_name = (
                            f"media:{media_bundle_name}:{field_name}"
                        )
                        if (
                            fully_qualified_field_name in row
                            and row[fully_qualified_field_name]
                        ):
                            media_track_fields_present = True
                            delimited_field_values = row[
                                fully_qualified_field_name
                            ].split(config["subdelimiter"])
                            for field_value in delimited_field_values:
                                if len(field_value.strip()):
                                    if validate_media_track_value(field_value) is False:
                                        message = (
                                            'Value in field "'
                                            + fully_qualified_field_name
                                            + '" in row with ID "'
                                            + row[config["id_field"]]
                                            + '" ('
                                            + field_value
                                            + ") has a media type is not a valid media track field value."
                                        )
                                        logging.error(message)
                                        sys.exit("Error: " + message)

                                    # Confirm that the media bundle name in the column header matches the media type
                                    # of the file in the 'file' column.
                                    file_media_type = set_media_type(
                                        config, row["file"], "file", row
                                    )
                                    if file_media_type != media_bundle_name:
                                        message = (
                                            'File named in the "file" field in row with ID "'
                                            + row[config["id_field"]]
                                            + '" ('
                                            + row["file"]
                                            + ") has a media type "
                                            + "("
                                            + file_media_type
                                            + ') that differs from the media type indicated in the column header "'
                                            + fully_qualified_field_name
                                            + '" ('
                                            + media_bundle_name
                                            + ")."
                                        )
                                        logging.error(message)
                                        sys.exit("Error: " + message)

                                # Confirm that config['media_use_tid'] and row-level media_use_term is for Service File (http://pcdm.org/use#ServiceFile).
                                service_file_exists = service_file_present(
                                    config, row["media_use_tid"]
                                )
                                if service_file_exists is False:
                                    message = f"{row['media_use_tid']} cannot be used as a \"media_use_tid\" value in your CSV when creating media tracks."
                                    logging.error(message)
                                    sys.exit("Error: " + message)

    if media_track_fields_present is True:
        message = "OK, media track field values in the CSV file validate."
        print(message)
        logging.info(message)


def validate_media_track_value(media_track_value: str) -> bool:
    """Validates that the string in "media_track_value" has valid values in its subparts.
    Parameters
        ----------
        media_track_value : string
            The CSV value to validate.
        Returns
        -------
        boolean
            True if it does, False if not.
    """
    valid_kinds = ["subtitles", "descriptions", "metadata", "captions", "chapters"]
    parts = media_track_value.split(":", 3)
    # First part, the label, needs to have a length; second part needs to be one of the
    # values in 'valid_kinds'; third part needs to be a valid Drupal language code; the fourth
    # part needs to end in '.vtt'.
    if (
        len(parts) == 4
        and len(parts[0]) > 0
        and validate_language_code(parts[2])
        and parts[1] in valid_kinds
        and parts[3].lower().endswith(".vtt")
    ):
        return True
    else:
        return False


def validate_term_name_length(
    term_name: str, row_id: Union[int, str], column_name: str
) -> None:
    """Checks that the length of a term name does not exceed
    Drupal's 255 character length.
    Parameters
    :param term_name: str - The taxonomy term name.
    :param row_id: Union[int, str] - The ID of the CSV row.
    :param column_name: str - The name of the CSV column.
    """
    term_name = term_name.strip()
    if len(term_name) > 255:
        message = (
            'CSV field "'
            + column_name
            + '" in record with ID '
            + row_id
            + " contains a taxonomy term that exceeds Drupal's limit of 255 characters (length of term is "
            + str(len(term_name))
            + " characters)."
        )
        message_2 = ' Term provided in CSV is "' + term_name + '".'
        message_3 = " Please reduce the term's length to less than 256 characters."
        logging.error(message + message_2 + message_3)
        sys.exit("Error: " + message + " See the Workbench log for more information.")


def validate_node_created_date(
    config: dict, csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None]
) -> None:
    """Checks that date_string is in the format used by Drupal's 'created' node property,
    e.g., 2020-11-15T23:49:22+00:00. Also check to see if the date is in the future.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param csv_data: Generator - The CSV data.
    """
    for count, row in enumerate(csv_data, start=1):
        validate_node_created_date_action(config, row)

    message = 'OK, dates in the "created" CSV field are all formated correctly and in the future.'
    print(message)
    logging.info(message)


def validate_edtf_fields(
    config: dict,
    field_definitions: dict,
    csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None],
) -> None:
    """Validate values in fields that are of type 'edtf'.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: Generator - The CSV data.
    """
    edtf_fields_present = False
    for count, row in enumerate(csv_data, start=1):
        for field_name in field_definitions.keys():
            if field_definitions[field_name]["field_type"] == "edtf":
                if field_name in row:
                    edtf_fields_present = True
                    delimited_field_values = row[field_name].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()):
                            valid = validate_edtf_date(field_value)
                            if valid is False:
                                message = (
                                    'Value in field "'
                                    + field_name
                                    + '" in row with ID '
                                    + row[config["id_field"]]
                                    + ' ("'
                                    + field_value
                                    + '") is not a valid EDTF date/time.'
                                )
                                logging.error(message)
                                if config["perform_soft_checks"] is False:
                                    sys.exit("Error: " + message)

    if edtf_fields_present is True:
        message = "OK, EDTF field values in the CSV file validate."
        print(message)
        logging.info(message)


def validate_edtf_date(date: str) -> bool:
    """Validates that the value in 'date' is a valid EDTF date.
    Parameters
    :param date: string - The EDTF date.
    :return: boolean - True if it is valid, False if not.
    """
    date = date.strip()
    # 195X-01~
    # nnnX-nn~
    if re.match(r"^[1-2]\d\dX\-\d\d\~", date):
        return True
    # nnnX?
    elif re.match(r"^[1-2]\d\dX\?", date):
        return True
    # nnXX?
    elif re.match(r"^[1-2]\dXX\?", date):
        return True
    # nXXX?
    elif re.match(r"^[1-2]XXX\?", date):
        return True
    # nXXX~
    elif re.match(r"^[1-2]XXX\~", date):
        return True
    # nnXX~
    elif re.match(r"^[1-2]\dXX\~", date):
        return True
    # nnnX~
    elif re.match(r"^[1-2]\d\dX\~", date):
        return True
    # nXXX%
    elif re.match(r"^[1-2]XXX\%", date):
        return True
    # nnXX%
    elif re.match(r"^[1-2]\dXX\%", date):
        return True
    # nnnX%
    elif re.match(r"^[1-2]\d\dX\%", date):
        return True
    # XXXX?
    elif re.match(r"^XXXX\?", date):
        return True
    # XXXX~
    elif re.match(r"^XXXX\~", date):
        return True
    # XXXX%
    elif re.match(r"^XXXX\%", date):
        return True
    elif edtf_validate.valid_edtf.is_valid(date):
        return True
    else:
        return False


def validate_url_aliases(
    config: dict, csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None]
) -> None:
    """Checks that URL aliases don't already exist.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param csv_data: Generator - The CSV data.
    """
    for row in csv_data:
        validate_url_aliases_action(config, row)

    message = "OK, URL aliases do not already exist."
    print(message)
    logging.info(message)


def validate_node_uid(
    config: dict, csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None]
) -> None:
    """Checks that the user identified in the 'uid' field exists in Drupal. Note that
    this does not validate any permissions the user may have.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param csv_data: Generator - The CSV data.
    """
    for count, row in enumerate(csv_data, start=1):
        validate_node_uid_action(config, row)

    message = 'OK, user IDs in the "uid" CSV field all exist.'
    print(message)
    logging.info(message)




def validate_parent_ids_in_csv_id_to_node_id_map(
    config: dict, csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None]
):
    """Query the CSV ID to node ID map to check for non-unique parent IDs.
    If they exist, report out but do not exit.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param csv_data: Generator - The CSV data.
    """
    if (
        len(config["csv_id_to_node_id_map_allowed_hosts"]) > 0
        and config["query_csv_id_to_node_id_map_for_parents"] is True
    ):
        csv_id_to_node_id_map_allowed_hosts_message = (
            f'Limiting lookups to those with the hostname "' + config["host"] + '".'
        )
    else:
        csv_id_to_node_id_map_allowed_hosts_message = (
            f'Not limiting lookups to those with the hostname "' + config["host"] + '".'
        )

    if config["query_csv_id_to_node_id_map_for_parents"] is True:
        message = f"Validating parent IDs in the CSV ID to node ID map, please wait. {csv_id_to_node_id_map_allowed_hosts_message}"
        print(message)
    else:
        return

    # First, confirm the database exists; if not, tell the user and exit.
    if config["csv_id_to_node_id_map_path"] is not False:
        if not os.path.exists(config["csv_id_to_node_id_map_path"]):
            message = f"Can't find CSV ID to node ID database path at {config['csv_id_to_node_id_map_path']}."
            logging.error(message)
            sys.exit("Error: " + message)

    # If database exists, query it.
    if (
        config["query_csv_id_to_node_id_map_for_parents"] is True
        and config["csv_id_to_node_id_map_path"] is not False
    ):
        id_field = config["id_field"]
        parents_from_id_map = []
        parents_from_id_map_warnings = []

        for row in csv_data:
            csv_id_to_node_id_map_allowed_hosts_sql = (
                get_csv_id_to_node_id_map_allowed_hosts_sql(config)
            )

            if config["ignore_duplicate_parent_ids"] is True:
                query = (
                    "select node_id from csv_id_to_node_id_map where "
                    + csv_id_to_node_id_map_allowed_hosts_sql
                    + f' csv_id = "{row["parent_id"]}" order by timestamp desc limit 1'
                )
            else:
                query = (
                    "select node_id from csv_id_to_node_id_map where"
                    + csv_id_to_node_id_map_allowed_hosts_sql
                    + " csv_id = ?"
                )
            parent_in_id_map_result = sqlite_manager(
                config,
                operation="select",
                query=query,
                db_file_path=config["csv_id_to_node_id_map_path"],
            )

            for parent_in_id_map_row in parent_in_id_map_result:
                parents_from_id_map.append(parent_in_id_map_row[id_field].strip())
            parents_from_id_map = list(dict.fromkeys(parents_from_id_map))
            if len(parents_from_id_map) > 1:
                parents_from_id_map_message = f'Query of ID map for parent ID "{row["parent_id"]}" returned multiple node IDs: ({", ".join(parents_from_id_map)}).'
                parents_from_id_map_warnings.append(parents_from_id_map_message)
        if len(parents_from_id_map_warnings) > 1:
            logging.warning(parents_from_id_map_warnings[-1])
            print("Warning: " + parents_from_id_map_warnings[-1])


def validate_taxonomy_field_values(
    config: dict, field_definitions: dict, csv_data: CsvReader
) -> Union[bool, None]:
    """Loop through all fields in field_definitions, and if a field
    is a taxonomy reference field, validate all values in the CSV
    data in that field against term IDs in the taxonomies referenced
    by the field. Does not validate Typed Relation fields
    (see validate_typed_relation_field_values()).
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: WorkbenchCsvReader - The CSV data.
    :return: Union[bool, None] - Returns None if no taxonomy fields are present; otherwise returns False.
    """
    # Define a list to store names of CSV fields that reference vocabularies.
    fields_with_vocabularies = list()
    # TODO: vocab_validation_issues is set here and then used at the bottom but never changed, could just return False
    vocab_validation_issues = False
    # Get all the term IDs for vocabularies referenced in all fields in the CSV.
    for column_name in csv_data.get_field_names():
        if column_name in field_definitions:
            if (
                field_definitions[column_name]["field_type"] == "entity_reference"
                and "vocabularies" in field_definitions[column_name]
            ):
                vocabularies = get_field_vocabularies(
                    config, field_definitions, column_name
                )
                # If there are no vocabularies linked to the current field, 'vocabularies'
                # will be False and will throw a TypeError.
                try:
                    num_vocabs = len(vocabularies)
                    if num_vocabs > 0:
                        fields_with_vocabularies.append(column_name)
                except BaseException:
                    message = (
                        'Workbench cannot get vocabularies linked to field "'
                        + column_name
                        + '". Please confirm that field has at least one vocabulary.'
                    )
                    logging.error(message)
                    sys.exit("Error: " + message)

    # If none of the CSV fields are taxonomy reference fields, return.
    if len(fields_with_vocabularies) == 0:
        return None

    # Iterate through the CSV and validate each taxonomy field's values.
    new_term_names_in_csv_results = []
    for count, row in enumerate(csv_data.get_csv_data(), start=1):
        for column_name in fields_with_vocabularies:
            if len(row[column_name]):
                new_term_names_in_csv = validate_taxonomy_reference_value(
                    config,
                    field_definitions,
                    column_name,
                    row[column_name],
                    row[config["id_field"]],
                )
                new_term_names_in_csv_results.append(new_term_names_in_csv)

    if True in new_term_names_in_csv_results and config["allow_adding_terms"] is True:
        if config["validate_terms_exist"] is True:
            message = (
                "OK, term IDs/names in CSV file exist in their respective taxonomies"
            )
            if config["log_term_creation"] is True:
                message = (
                    message
                    + " (new terms will be created as noted in the Workbench log)."
                )
            else:
                message = (
                    message
                    + ' (new terms will be created but not noted in the Workbench log since "log_term_creation" is set to false).'
                )
            print(message)
        else:
            if config["log_term_creation"] is True:
                print(
                    "Skipping check for existence of terms (new terms will be created as noted in the Workbench log)."
                )
            else:
                print(
                    'Skipping check for existence of terms (notee: terms will be created but not noted in the Workbench log - "log_term_creation" is set to false).'
                )
            logging.warning(
                "Skipping check for existence of terms (but new terms will be created)."
            )
    else:
        # All term IDs are in their field's vocabularies.
        print("OK, term IDs/names in CSV file exist in their respective taxonomies.")
        logging.info(
            "OK, term IDs/names in CSV file exist in their respective taxonomies."
        )

    return vocab_validation_issues


def validate_vocabulary_fields_in_csv(
    config: dict, vocabulary_id: str, vocab_csv_file_path: str
) -> None:
    """Loop through all fields in CSV to ensure that all present fields match field
    from the vocab's field definitions, and that any required fields are present.
    Also checks that each row has the same number of columns as there are headers.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        vocabulary_id : string
            The ID of the vocabulary to validate.
        vocab_csv_file_path: string
            Location of vocabulary CSV file.
    """
    csv_reader = CsvReader(config, "taxonomy_fields", vocab_csv_file_path)
    csv_data = csv_reader.get_csv_data()
    csv_column_headers = copy.copy(csv_reader.get_field_names())

    # Check whether each row contains the same number of columns as there are headers.
    row_count = 0
    for count, row in enumerate(csv_data, start=1):
        extra_headers = False
        field_count = 0
        row_count += 1
        for field in row:
            # 'stringtopopulateextrafields' is added by get_csv_data() if there are extra headers.
            if row[field] == "stringtopopulateextrafields":
                extra_headers = True
            else:
                field_count += 1
        if extra_headers is True:
            message = (
                "Row with ID "
                + row[config["id_field"]]
                + ') of the vocabulary CSV file "'
                + vocab_csv_file_path
                + '" has fewer columns than there are headers ('
                + str(len(csv_column_headers))
                + ")."
            )
            logging.error(message)
            sys.exit("Error: " + message)
        # Note: this message is also generated in get_csv_data() since CSV Writer thows an exception if the row has
        # form fields than headers.
        if len(csv_column_headers) < field_count:
            message = (
                "Row with term name '"
                + row["term_name"]
                + ') of the vocabulary CSV file "'
                + vocab_csv_file_path
                + '" has more columns ('
                + str(field_count)
                + ") than there are headers ("
                + str(len(csv_column_headers))
                + ")."
            )
            logging.error(message)
            sys.exit("Error: " + message)
    message = (
        "OK, all "
        + str(row_count)
        + ' rows in the vocabulary CSV file "'
        + vocab_csv_file_path
        + '" have the same number of columns as there are headers ('
        + str(len(csv_column_headers))
        + ")."
    )
    print(message)
    logging.info(message)

    # Check that the required 'term_name' and 'parent' columns are present in the CSV, and that
    # any fields defined as required in the vocabulary are also present.
    field_definitions = get_field_definitions(
        config, "taxonomy_term", vocabulary_id.strip()
    )

    # Check that 'term_name' and 'parent' are in the CSV.
    if "term_name" not in csv_column_headers:
        message = (
            'Required column "term_name" not found in vocabulary CSV file "'
            + vocab_csv_file_path
            + '".'
        )
        logging.error(message)
        sys.exit("Error: " + message)
    if "parent" not in csv_column_headers:
        message = (
            'Required column "parent" not found in vocabulary CSV file "'
            + vocab_csv_file_path
            + '".'
        )
        logging.error(message)
        sys.exit("Error: " + message)
    # Then vocabulary fields that are defined as required.
    field_definition_fieldnames = field_definitions.keys()
    for field in field_definition_fieldnames:
        if (
            field_definitions[field]["required"] is True
            and field not in csv_column_headers
        ):
            message = (
                'Required column "'
                + field
                + '" not found in vocabulary CSV file "'
                + vocab_csv_file_path
                + '".'
            )
            logging.error(message)
            sys.exit("Error: " + message)

    # Check whether remaining fields in the vocabulary CSV are fields defined in the current vocabulary.
    if "field_name" in csv_column_headers:
        csv_column_headers.remove("field_name")
    if "parent" in csv_column_headers:
        csv_column_headers.remove("parent")
    for csv_field in csv_column_headers:
        if csv_field not in field_definitions.keys():
            message = (
                'CSV column "'
                + csv_field
                + '" in vocabulary CSV file "'
                + vocab_csv_file_path
                + '" is not a field in the "'
                + vocabulary_id
                + '" vocabulary.'
            )
            logging.error(message)
            sys.exit("Error: " + message)


def validate_typed_relation_field_values(
    config: dict, field_definitions: dict, csv_data: CsvReader
) -> Union[bool, None]:
    """Validate values in fields that are of type 'typed_relation'. Each CSV
    value must have this pattern: "string:string:int" or "string:string:string".
    If the last segment is a string, it must be term name, a namespaced term name,
    or an http URI.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: WorkbenchCsvReader - The CSV data.
    :return: Union[bool, None] - Returns None if no typed relation fields are present; otherwise returns False.
    """
    # Define a list to store CSV field names that contain vocabularies.
    fields_with_vocabularies = list()
    # Get all the term IDs for vocabularies referenced in all fields in the CSV.
    # TODO: vocab_validation_issues is set here and then used at the bottom but never changed, could just return False
    vocab_validation_issues = False
    for column_name in csv_data.get_field_names():
        if column_name in field_definitions:
            if "vocabularies" in field_definitions[column_name]:
                vocabularies = get_field_vocabularies(
                    config, field_definitions, column_name
                )
                # If there are no vocabularies linked to the current field, 'vocabularies'
                # will be False and will throw a TypeError.
                try:
                    num_vocabs = len(vocabularies)
                    if num_vocabs > 0:
                        fields_with_vocabularies.append(column_name)
                except BaseException:
                    message = (
                        'Workbench cannot get vocabularies linked to field "'
                        + column_name
                        + '". Please confirm that field has at least one vocabulary.'
                    )
                    logging.error(message)
                    sys.exit("Error: " + message)

    # If none of the CSV fields are taxonomy reference fields, return.
    if len(fields_with_vocabularies) == 0:
        return None

    typed_relation_fields_present = False
    new_term_names_in_csv_results = []
    for count, row in enumerate(csv_data.get_csv_data(), start=1):
        for field_name in field_definitions.keys():
            if (
                field_definitions[field_name]["field_type"] == "typed_relation"
                and "typed_relations" in field_definitions[field_name]
            ):
                if field_name in row:
                    typed_relation_fields_present = True
                    delimited_field_values = row[field_name].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value) == 0:
                            continue
                        # First check the required patterns.
                        if not re.match(
                            "^[0-9a-zA-Z]+:[0-9a-zA-Z]+:.+$", field_value.strip()
                        ):
                            message = (
                                'Value in field "'
                                + field_name
                                + '" in row with ID '
                                + row[config["id_field"]]
                                + " ("
                                + field_value
                                + ") does not use the structure required for typed relation fields."
                            )
                            logging.error(message)
                            sys.exit("Error: " + message)

                        # Then, check to see if the relator string (the first two parts of the
                        # value) exist in the field_definitions[fieldname]['typed_relations'] list.
                        typed_relation_value_parts = field_value.split(":", 2)
                        relator_string = (
                            typed_relation_value_parts[0]
                            + ":"
                            + typed_relation_value_parts[1]
                        )
                        if (
                            relator_string
                            not in field_definitions[field_name]["typed_relations"]
                        ):
                            message = (
                                'Value in field "'
                                + field_name
                                + '" in row with ID '
                                + row[config["id_field"]]
                                + " contains a relator ("
                                + relator_string
                                + ") that is not configured for that field."
                            )
                            logging.error(message)
                            sys.exit("Error: " + message)

                    # Iterate through the CSV and validate the taxonomy term/name/URI in each field subvalue.
                    for column_name in fields_with_vocabularies:
                        if len(row[column_name]):
                            delimited_field_values = row[column_name].split(
                                config["subdelimiter"]
                            )
                            delimited_field_values_without_relator_strings = []
                            for field_value in delimited_field_values:
                                # Strip the relator string out from field_value, leaving the vocabulary ID and term ID/name/URI.
                                term_to_check = re.sub(
                                    "^[0-9a-zA-Z]+:[0-9a-zA-Z]+:", "", field_value
                                )
                                delimited_field_values_without_relator_strings.append(
                                    term_to_check
                                )

                            field_value_to_check = config["subdelimiter"].join(
                                delimited_field_values_without_relator_strings
                            )
                            new_term_names_in_csv = validate_taxonomy_reference_value(
                                config,
                                field_definitions,
                                column_name,
                                field_value_to_check,
                                row[config["id_field"]],
                            )
                            new_term_names_in_csv_results.append(new_term_names_in_csv)

    if (
        typed_relation_fields_present is True
        and True in new_term_names_in_csv_results
        and config["allow_adding_terms"] is True
    ):
        message = "OK, term IDs/names used in typed relation fields in the CSV file exist in their respective taxonomies"
        if config["log_term_creation"] is True:
            message = (
                message + " (new terms will be created as noted in the Workbench log)."
            )
        else:
            message = (
                message
                + ' (new terms will be created but not noted in the Workbench log since "log_term_creation" is set to false).'
            )
        print(message)
    else:
        if typed_relation_fields_present is True:
            # All term IDs are in their field's vocabularies.
            print(
                "OK, term IDs/names used in typed relation fields in the CSV file exist in their respective taxonomies."
            )
            logging.info(
                "OK, term IDs/names used in typed relation fields in the CSV file exist in their respective taxonomies."
            )

    return vocab_validation_issues


def validate_taxonomy_reference_value(
    config: dict,
    field_definitions: dict,
    csv_field_name: str,
    csv_field_value: str,
    row_id: Union[int, str],
) -> Union[bool, None]:
    """Validate that the value(s) in 'csv_field_value' exist in the vocabularies
    referenced by the field 'csv_field_name'.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_field_name: str - The name of the CSV field to validate.
    :param csv_field_value: str - The value from the CSV field to validate.
    :param row_id: Union[int, str] - The ID of the current row.
    :return: Union[bool, None] - Returns None if no taxonomy reference fields, True if new term names are present; otherwise False.
    """

    this_fields_vocabularies = get_field_vocabularies(
        config, field_definitions, csv_field_name
    )

    # Not an entity reference field on a vocabulary or a typed relation field.
    if (
        field_definitions[csv_field_name]["field_type"]
        in ["entity_reference", "typed_relation"]
        and "vocabularies" in field_definitions[csv_field_name]
    ) is False:
        return None

    this_fields_vocabularies_string = ", ".join(this_fields_vocabularies)

    new_term_names_in_csv = False

    # Allow for multiple values in one field.
    terms_to_check = csv_field_value.split(config["subdelimiter"])
    for field_value in terms_to_check:
        # If this is a multi-taxonomy field, all term names (not IDs or URIs) must be namespaced using the vocab_id:term_name pattern,
        # regardless of whether config['allow_adding_terms'] is True. Also, we need to accommodate terms that are namespaced
        # and also contain a ':'.
        if (
            len(this_fields_vocabularies) > 1
            and value_is_numeric(field_value) is False
            and not field_value.startswith("http")
        ):
            split_field_values = field_value.split(config["subdelimiter"])
            for split_field_value in split_field_values:
                if ":" in field_value:
                    # If the : is present, validate that the namespace is one of the vocabulary IDs referenced by this field.
                    [tentative_namespace, tentative_term_name] = field_value.split(
                        ":", 1
                    )
                    if tentative_namespace not in this_fields_vocabularies:
                        message = (
                            'Vocabulary ID "'
                            + tentative_namespace
                            + '" used in CSV column "'
                            + csv_field_name
                            + '", row with ID '
                            + str(row_id)
                            + " does not match any of the vocabularies referenced by the"
                            + " corresponding Drupal field ("
                            + this_fields_vocabularies_string
                            + ")."
                        )
                        logging.error(message)
                        sys.exit("Error: " + message)
                else:
                    message = (
                        'Term names in CSV field "'
                        + csv_field_name
                        + '" require a vocabulary namespace; CSV value '
                    )
                    message_2 = (
                        '"'
                        + field_value
                        + '" in row with ID '
                        + str(row_id)
                        + " does not have one."
                    )
                    logging.error(message + message_2)
                    sys.exit("Error: " + message + message_2)

                validate_term_name_length(
                    split_field_value, str(row_id), csv_field_name
                )

        # Check to see if field_value is a member of the field's vocabularies. First, check whether field_value is a term ID.
        if (
            value_is_numeric(field_value)
            and csv_field_name not in config["columns_with_term_names"]
        ):
            field_value = field_value.strip()
            term_in_vocabs = False
            for vocab_id in this_fields_vocabularies:
                term_vocab = get_term_vocab(config, field_value)
                if term_vocab == vocab_id:
                    term_in_vocabs = True
            if term_in_vocabs is False:
                message = (
                    'CSV field "'
                    + csv_field_name
                    + '" in row with ID '
                    + str(row_id)
                    + " contains a term ID ("
                    + field_value
                    + ") that is "
                )
                if len(this_fields_vocabularies) > 1:
                    message_2 = (
                        "not in one of the referenced vocabularies ("
                        + this_fields_vocabularies_string
                        + ")."
                    )
                else:
                    message_2 = (
                        'not in the referenced vocabulary ("'
                        + this_fields_vocabularies[0]
                        + '").'
                    )
                logging.error(message + message_2)
                sys.exit("Error: " + message + message_2)
        # Then check values that are URIs.
        elif field_value.strip().startswith("http"):
            field_value = field_value.strip()
            tid_from_uri = get_term_id_from_uri(config, field_value)
            if value_is_numeric(tid_from_uri):
                term_vocab = get_term_vocab(config, tid_from_uri)
                term_in_vocabs = False
                for vocab_id in this_fields_vocabularies:
                    if term_vocab == vocab_id:
                        term_in_vocabs = True
                if term_in_vocabs is False:
                    message = (
                        'CSV field "'
                        + csv_field_name
                        + '" in row with ID '
                        + str(row_id)
                        + " contains a term URI ("
                        + field_value
                        + ") that is "
                    )
                    if len(this_fields_vocabularies) > 1:
                        message_2 = (
                            "not in one of the referenced vocabularies ("
                            + this_fields_vocabularies_string
                            + ")."
                        )
                    else:
                        message_2 = (
                            'not in the referenced vocabulary ("'
                            + this_fields_vocabularies[0]
                            + '").'
                        )
                    logging.error(message + message_2)
                    sys.exit("Error: " + message + message_2)
            else:
                message = (
                    'Term URI "'
                    + field_value
                    + '" used in CSV column "'
                    + csv_field_name
                    + '" row with ID '
                    + str(row_id)
                    + " does not match any terms."
                )
                logging.error(message)
                sys.exit("Error: " + message)
        # Finally, check values that are string term names.
        else:
            new_terms_to_add = []
            for vocabulary in this_fields_vocabularies:
                tid = find_term_in_vocab(config, vocabulary, field_value)
                if value_is_numeric(tid) is False:
                    # Single taxonomy fields.
                    if len(this_fields_vocabularies) == 1:
                        if config["allow_adding_terms"] is True:
                            # Warn if namespaced term name is not in specified vocab.
                            if tid is False:
                                new_term_names_in_csv = True
                                validate_term_name_length(
                                    field_value, str(row_id), csv_field_name
                                )
                                message = (
                                    'CSV field "'
                                    + csv_field_name
                                    + '" in row with ID '
                                    + str(row_id)
                                    + ' contains a term ("'
                                    + field_value.strip()
                                    + '") that is '
                                )

                                if (
                                    this_fields_vocabularies[0]
                                    in config["protected_vocabularies"]
                                ):
                                    message_2 = (
                                        'not in the referenced vocabulary ("'
                                        + this_fields_vocabularies[0]
                                        + '"). The term will not be created since "'
                                        + this_fields_vocabularies[0]
                                        + '" is registered in the "protected_vocabularies" config setting.'
                                    )
                                else:
                                    message_2 = (
                                        'not in the referenced vocabulary ("'
                                        + this_fields_vocabularies[0]
                                        + '"). That term will be created.'
                                    )
                                if config["validate_terms_exist"] is True:
                                    logging.warning(message + message_2)
                        else:
                            new_term_names_in_csv = True
                            message = (
                                'CSV field "'
                                + csv_field_name
                                + '" in row with ID '
                                + str(row_id)
                                + ' contains a term ("'
                                + field_value.strip()
                                + '") that is '
                            )
                            message_2 = (
                                'not in the referenced vocabulary ("'
                                + this_fields_vocabularies[0]
                                + '").'
                            )
                            logging.error(message + message_2)
                            sys.exit("Error: " + message + message_2)

                # If this is a multi-taxonomy field, all term names must be namespaced using the vocab_id:term_name pattern,
                # regardless of whether config['allow_adding_terms'] is True.
                if len(this_fields_vocabularies) > 1:
                    split_field_values = field_value.split(config["subdelimiter"])
                    for split_field_value in split_field_values:
                        # Check to see if the namespaced vocab is referenced by this field.
                        [namespace_vocab_id, namespaced_term_name] = (
                            split_field_value.split(":", 1)
                        )
                        if namespace_vocab_id not in this_fields_vocabularies:
                            message = (
                                'CSV field "'
                                + csv_field_name
                                + '" in row with ID '
                                + str(row_id)
                                + " contains a namespaced term name "
                            )
                            message_2 = (
                                '("'
                                + namespaced_term_name.strip()
                                + '") that specifies a vocabulary not associated with that field ('
                                + namespace_vocab_id
                                + ")."
                            )
                            logging.error(message + message_2)
                            sys.exit("Error: " + message + message_2)

                        tid = find_term_in_vocab(
                            config, namespace_vocab_id, namespaced_term_name
                        )

                        # Warn if namespaced term name is not in specified vocab.
                        if config["allow_adding_terms"] is True:
                            if (
                                tid is False
                                and split_field_value not in new_terms_to_add
                            ):
                                new_term_names_in_csv = True
                                message = (
                                    'CSV field "'
                                    + csv_field_name
                                    + '" in row with ID '
                                    + str(row_id)
                                    + ' contains a term ("'
                                    + namespaced_term_name.strip()
                                    + '") that is '
                                )

                                if (
                                    namespace_vocab_id
                                    in config["protected_vocabularies"]
                                ):
                                    message_2 = (
                                        'not in the referenced vocabulary ("'
                                        + namespace_vocab_id
                                        + '"). The term will not be created since "'
                                        + namespace_vocab_id
                                        + '" is registered in the "protected_vocabularies" config setting.'
                                    )
                                else:
                                    message_2 = (
                                        'not in the referenced vocabulary ("'
                                        + namespace_vocab_id
                                        + '"). That term will be created.'
                                    )
                                if config["validate_terms_exist"] is True:
                                    logging.warning(message + message_2)
                                new_terms_to_add.append(split_field_value)

                                validate_term_name_length(
                                    split_field_value,
                                    str(row_id),
                                    csv_field_name,
                                )
                        # Die if namespaced term name is not specified vocab.
                        else:
                            if tid is False:
                                message = (
                                    'CSV field "'
                                    + csv_field_name
                                    + '" in row with ID '
                                    + str(row_id)
                                    + ' contains a term ("'
                                    + namespaced_term_name.strip()
                                    + '") that is '
                                )
                                message_2 = (
                                    'not in the referenced vocabulary ("'
                                    + namespace_vocab_id
                                    + '").'
                                )
                                logging.warning(message + message_2)
                                sys.exit("Error: " + message + message_2)

    return new_term_names_in_csv


def validate_numeric_fields(
    config: dict,
    field_definitions: dict,
    csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None],
) -> None:
    """Validate integer, decimal, and float fields.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: Generator - The CSV data.
    """
    numeric_fields_present = False
    for count, row in enumerate(csv_data, start=1):
        for field_name in field_definitions.keys():
            if field_definitions[field_name]["field_type"] == "integer":
                if field_name in row:
                    numeric_fields_present = True
                    delimited_field_values = row[field_name].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()):
                            if not value_is_numeric(field_value.strip()):
                                message = (
                                    'Value in field "'
                                    + field_name
                                    + '" in row with ID '
                                    + row[config["id_field"]]
                                    + " ("
                                    + field_value
                                    + ") is not a valid integer value."
                                )
                                logging.error(message)
                                sys.exit("Error: " + message)
            if field_definitions[field_name]["field_type"] in ["decimal", "float"]:
                if field_name in row:
                    numeric_fields_present = True
                    delimited_field_values = row[field_name].split(
                        config["subdelimiter"]
                    )
                    for field_value in delimited_field_values:
                        if len(field_value.strip()):
                            if not value_is_numeric(
                                field_value.strip(), allow_decimals=True
                            ):
                                message = (
                                    'Value in field "'
                                    + field_name
                                    + '" in row with ID '
                                    + row[config["id_field"]]
                                    + " ("
                                    + field_value
                                    + ") is not a valid "
                                    + field_definitions[field_name]["field_type"]
                                    + " value."
                                )
                                logging.error(message)
                                sys.exit("Error: " + message)

    if numeric_fields_present is True:
        message = "OK, numeric field values in the CSV file validate."
        print(message)
        logging.info(message)


def validate_csv_field_length(
    config: dict, field_definitions: dict, csv_data: CsvReader
) -> None:
    """Compare values in the CSV data with the fields' max_length. Log CSV
    fields that exceed their max_length, and warn user if
    these fields exist in their CSV data.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: WorkbenchCsvReader - The CSV data.
    """
    field_max_lengths = dict()
    csv_headers = csv_data.get_field_names()
    for csv_header in csv_headers:
        if csv_header in field_definitions.keys():
            if "max_length" in field_definitions[csv_header]:
                max_length = field_definitions[csv_header]["max_length"]
                # We don't care about max_length of None (i.e., it's not applicable or unlimited).
                if max_length is not None:
                    field_max_lengths[csv_header] = max_length

    for count, row in enumerate(csv_data.get_csv_data(), start=1):
        for field_name in field_max_lengths.keys():
            if field_name in row:
                delimited_field_values = row[field_name].split(config["subdelimiter"])
                for field_value in delimited_field_values:
                    if field_name in field_max_lengths and len(field_value) > int(
                        field_max_lengths[field_name]
                    ):
                        message = ""
                        if config["task"] == "create":
                            message = (
                                'CSV field "'
                                + field_name
                                + '" in record with ID '
                                + row[config["id_field"]]
                                + " contains a value that is longer ("
                                + str(len(field_value))
                                + " characters)"
                            )
                        elif config["task"] == "update":
                            message = (
                                'CSV field "'
                                + field_name
                                + '" in record with node ID '
                                + row["node_id"]
                                + " contains a value that is longer ("
                                + str(len(field_value))
                                + " characters)"
                            )
                        message_2 = (
                            " than allowed for that field ("
                            + str(field_max_lengths[field_name])
                            + " characters). Workbench will truncate this value prior to populating Drupal."
                        )
                        print("Warning: " + message + message_2)
                        logging.warning(message + message_2)


def validate_text_list_fields(
    config: dict, field_definitions: dict, csv_data: CsvReader
) -> None:
    """For fields that are of "list_string" field type, check that values
    in CSV are in the field's "allowed_values" config setting.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: WorkbenchCsvReader - The CSV data.
    """
    list_field_allowed_values = dict()
    csv_headers = csv_data.get_field_names()
    for csv_header in csv_headers:
        if csv_header in field_definitions.keys():
            if "allowed_values" in field_definitions[csv_header]:
                if field_definitions[csv_header]["allowed_values"] is not None:
                    list_field_allowed_values[csv_header] = field_definitions[
                        csv_header
                    ]["allowed_values"]

    for count, row in enumerate(csv_data.get_csv_data(), start=1):
        for field_name in list_field_allowed_values.keys():
            if field_name in row and len(row[field_name]) > 0:
                delimited_field_values = row[field_name].split(config["subdelimiter"])
                for field_value in delimited_field_values:
                    if (
                        field_name in list_field_allowed_values
                        and field_value not in list_field_allowed_values[field_name]
                    ):
                        if config["task"] == "create":
                            message = (
                                'CSV field "'
                                + field_name
                                + '" in record with ID '
                                + row[config["id_field"]]
                                + ' contains a value ("'
                                + field_value
                                + "\") that is not in the fields's allowed values."
                            )
                        if config["task"] == "update":
                            message = (
                                'CSV field "'
                                + field_name
                                + '" in record with node ID '
                                + row[config["id_field"]]
                                + ' contains a value ("'
                                + field_value
                                + "\") that is not in the fields's allowed values."
                            )
                        print("Warning: " + message)
                        logging.warning(message)


def validate_csv_field_cardinality(
    config: dict, field_definitions: dict, csv_data: CsvReader
) -> None:
    """Compare values in the CSV data with the fields' cardinality. Log CSV
    fields that have more values than allowed, and warn user if
    these fields exist in their CSV data.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param field_definitions: dict - The dictionary of field definitions.
    :param csv_data: WorkbenchCsvReader - The CSV data.
    """
    field_cardinalities = dict()
    csv_headers = csv_data.get_field_names()
    for csv_header in csv_headers:
        if csv_header in field_definitions.keys():
            cardinality = field_definitions[csv_header]["cardinality"]
            # We don't care about cardinality of -1 (unlimited).
            if int(cardinality) > 0:
                field_cardinalities[csv_header] = cardinality

    for count, row in enumerate(csv_data.get_csv_data(), start=1):
        for field_name in field_cardinalities.keys():
            if field_name in row:
                # Don't check for the subdelimiter in title.
                if field_name == "title":
                    continue
                delimited_field_values = row[field_name].split(config["subdelimiter"])
                if (
                    field_cardinalities[field_name] == 1
                    and len(delimited_field_values) > 1
                ):
                    message = ""
                    if config["task"] == "create":
                        message = (
                            'CSV field "'
                            + field_name
                            + '" in record with ID '
                            + row[config["id_field"]]
                            + " contains more values than the number "
                        )
                    elif config["task"] == "update":
                        message = (
                            'CSV field "'
                            + field_name
                            + '" in record with node ID '
                            + row["node_id"]
                            + " contains more values than the number "
                        )
                    message_2 = (
                        "allowed for that field ("
                        + str(field_cardinalities[field_name])
                        + "). Workbench will add only the first value."
                    )
                    print("Warning: " + message + message_2)
                    logging.warning(message + message_2)
                if (
                    int(field_cardinalities[field_name]) > 1
                    and len(delimited_field_values) > field_cardinalities[field_name]
                ):
                    message = ""
                    if config["task"] == "create":
                        message = (
                            'CSV field "'
                            + field_name
                            + '" in record with ID '
                            + row[config["id_field"]]
                            + " contains more values than the number "
                        )
                    elif config["task"] == "update":
                        message = (
                            'CSV field "'
                            + field_name
                            + '" in record with node ID '
                            + row["node_id"]
                            + " contains more values than the number "
                        )
                    message_2 = (
                        "allowed for that field ("
                        + str(field_cardinalities[field_name])
                        + "). Workbench will add only the first "
                        + str(field_cardinalities[field_name])
                        + " values."
                    )
                    print("Warning: " + message + message_2)
                    logging.warning(message + message_2)


def validate_required_fields_have_values_action(
    config: dict, required_fields: list, row: dict
) -> None:
    """Check that required fields have values in the given CSV row. If not, log an error and exit.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param required_fields: list - List of required field names.
    :param row: dict - The CSV row.

    :raise WorkbenchValidationException: If a required field is empty in the given row.
    """
    for required_field in required_fields:
        if (
            required_field == "field_model"
            and config["paged_content_from_directories_parents_exist"] is True
            and "paged_content_page_model_tid" in config.keys()
            and config["paged_content_page_model_tid"] is not None
        ):
            continue
        if required_field not in row or len(row[required_field].strip()) == 0:
            message = f"Required Drupal field \"{required_field}\" in row with ID \"{row[config['id_field']]}\" is empty."
            raise WorkbenchValidationException(message)


def validate_required_fields_have_values(
    config: dict,
    required_drupal_fields: list,
    csv_data: Generator[dict[Union[str, Any], Union[str, Any]], Any, None],
) -> None:
    """Loop through all fields in CSV to ensure that required field have a value in the CSV.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param required_drupal_fields: list - List of required Drupal field names.
    :param csv_data: DictReader - The CSV data.
    """
    rows_with_missing_required_values = []
    for row in csv_data:
        try:
            validate_required_fields_have_values_action(
                config, required_drupal_fields, row
            )
        except WorkbenchValidationException as e:
            rows_with_missing_required_values.append(str(e))

    if len(rows_with_missing_required_values) > 0:
        sys.exit(
            "Error: "
            + "Some required Drupal fields in your CSV file are empty. See log for more information."
        )


def validate_input_dir(config: dict) -> None:
    """Check that the input directory specified in the configuration exists. If not,
    log an error and exit.
    Parameters
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    """
    # Check existence input directory.
    if os.path.isabs(config["input_dir"]):
        input_dir_path = config["input_dir"]
    else:
        input_dir_path = os.path.abspath(config["input_dir"])
    if not os.path.exists(input_dir_path):
        message = (
            'Input directory specified in the "input_dir" configuration setting ("'
            + config["input_dir"]
            + '") not found.'
        )
        logging.error(message)
        sys.exit("Error: " + message)

def validate_entity_reference_view_endpoints(ctx: dict, csv_column_headers: list) -> None:
    """Validate that the entity reference view endpoints specified in the configuration are accessible and that the corresponding CSV columns exist."""
    # Check the configuration that is necessary for enabling use of term names in Entity Reference Views fields.
    for entity_reference_view_field_name, entity_reference_view_endpoint in ctx["config"][
        "entity_reference_view_endpoints"].items():
        if entity_reference_view_field_name not in csv_column_headers:
            message = f'CSV column {entity_reference_view_field_name} identified in "entity_reference_view_endpoints" is not in your CSV file.'
            ctx["log"].error(message)
            raise WorkbenchValidationException(message)
        view_url = f'{ctx["config"]["host"]}/{entity_reference_view_endpoint.lstrip("/")}'
        view_path_status_code = ping_view_endpoint(ctx["config"], view_url)
        if view_path_status_code != 200:
            message = f'Cannot access View REST export configured in "entity_reference_view_endpoints" ({view_url}).'
            ctx["log"].error(message)
            raise WorkbenchValidationException(message)
        else:
            message = f'View REST export configured in "entity_reference_view_endpoints" ({view_url}) is accessible.'
            ctx["log"].info(message)


def validate_media_use_tid(
    config: dict,
    media_use_tid_value_from_csv: Union[int, str] = None,
    csv_row_id: Union[int, str] = None,
) -> None:
    """Validate whether the term ID, term name, or terms URI provided in the
    config value for media_use_tid is in the Islandora Media Use vocabulary.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param media_use_tid_value_from_csv: int or str - The term ID, term name, or term URI to validate.
    :param csv_row_id: int or str - The CSV row ID, if applicable.

    :raise WorkbenchValidationException: If the term ID, term name, or term URI is not found in the Islandora Media Use vocabulary.
    """
    if (
        media_use_tid_value_from_csv is not None
        and csv_row_id is not None
        and len(str(media_use_tid_value_from_csv)) > 0
    ):
        media_use_tid_value = media_use_tid_value_from_csv
        message_wording = ' in the CSV "media_use_tid" column '
    else:
        media_use_tid_value = config["media_use_tid"]
        message_wording = ' in configuration option "media_use_tid" '

    media_use_terms = str(media_use_tid_value).split(config["subdelimiter"])
    for media_use_term in media_use_terms:
        if value_is_numeric(
            media_use_term
        ) is not True and media_use_term.strip().startswith("http"):
            media_use_tid = get_term_id_from_uri(config, media_use_term.strip())
            row_id_msg = (
                f" {message_wording}"
                if csv_row_id is None
                else f' provided in "media_use_tid" field in CSV row {str(csv_row_id)}'
            )
            if media_use_tid is False:
                message = f'URI "{media_use_term}" provided{row_id_msg} does not match any taxonomy terms.'
                logging.error(message)
                raise WorkbenchValidationException(message)
            elif media_use_term.strip() != "http://pcdm.org/use#OriginalFile":
                message = (
                    f'Warning: URI "{media_use_term}" provided{row_id_msg}'
                    + "will assign an Islandora Media Use term that might conflict with derivative media. "
                    + "You should temporarily disable the Context or Action that generates those derivatives."
                )
                print(message)
                logging.warning(message)
        elif not value_is_numeric(media_use_term):
            media_use_tid = find_term_in_vocab(
                config, "islandora_media_use", media_use_term.strip()
            )
            if media_use_tid is False:
                row_id_msg = (
                    " configuration option"
                    if csv_row_id is None
                    else f" field in CSV row {str(csv_row_id)}"
                )
                message = (
                    'Warning: Term name "'
                    + media_use_term.strip()
                    + f'" used in the "media_use_tid"{row_id_msg} does not match any taxonomy terms.'
                )
                logging.warning(message)
                raise WorkbenchValidationException(message)

        else:
            # Confirm the tid exists and is in the islandora_media_use vocabulary
            term_endpoint = (
                config["host"]
                + "/taxonomy/term/"
                + str(media_use_term.strip())
                + "?_format=json"
            )
            headers = {"Accept": "application/json"}
            response = issue_request(config, "GET", term_endpoint, headers)
            if response.status_code == 404:
                row_id_msg = (
                    " configuration option"
                    if csv_row_id is None
                    else f" field in CSV row {str(csv_row_id)}"
                )
                message = f'Warning: Term ID "{media_use_term}" used in the "media_use_tid"{row_id_msg} is not a term ID (term doesn\'t exist).'
                logging.error(message)
                raise WorkbenchValidationException(message)
            elif response.status_code == 200:
                response_body = response.json()
                row_id_msg = (
                    " configuration option"
                    if csv_row_id is None
                    else f" field in CSV row {str(csv_row_id)}"
                )
                if "vid" in response_body:
                    if response_body["vid"][0]["target_id"] != "islandora_media_use":
                        message = f'Term ID "{str(media_use_term)}" used in the "media_use_tid"{row_id_msg} is not in the Islandora Media Use vocabulary.'
                        logging.error(message)
                        raise WorkbenchValidationException(message)
                elif "field_external_uri" in response_body:
                    if (
                        response_body["field_external_uri"][0]["uri"]
                        != "http://pcdm.org/use#OriginalFile"
                    ):
                        message = (
                            f'Warning: Term ID "{media_use_term}" used in the "media_use_tid"{row_id_msg} '
                            + "will assign an Islandora Media Use term that might conflict with derivative media. "
                            + " You should temporarily disable the Context or Action that generates those derivatives."
                        )
                        print(message)
                        logging.warning(message)


def validate_media_use_tids_in_csv_action(config: dict, row: dict) -> None:
    """Validate 'media_use_tid' values in a single CSV row.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param row: dict - The CSV row.

    :raise WorkbenchValidationException: If the term ID, term name, or term URI is not found in the Islandora Media Use vocabulary.
    """
    if config["task"] == "add_media":
        csv_id_field = "node_id"
    else:
        csv_id_field = config["id_field"]

    if "media_use_tid" in row:
        delimited_field_values = row["media_use_tid"].split(config["subdelimiter"])
        for field_value in delimited_field_values:
            if len(field_value.strip()) > 0:
                validate_media_use_tid(config, field_value, row[csv_id_field])


def validate_media_use_tid_in_additional_files_setting(
    config: dict, media_use_tid_value: Union[int, str], additional_field_name: str
) -> None:
    """Validate whether the term ID registered in the "additional_files" config setting
    is in the Islandora Media Use vocabulary.
    :param config: dict - The configuration settings defined by WorkbenchConfig.get_config().
    :param media_use_tid_value: int or str - The term ID, term name, or term URI to validate.
    :param additional_field_name: str - The name of the field in the "additional_files" config setting.
    :raise WorkbenchValidationException: If the term ID, term name, or term URI is not found in the Islandora Media Use vocabulary.
    :raise WorkbenchValidationWarning: If the term ID, term name, or term URI is found in the Islandora Media Use vocabulary but may conflict with derivative media.
    """
    media_use_tids = []
    if config["subdelimiter"] in str(media_use_tid_value):
        media_use_tids = str(media_use_tid_value).split(config["subdelimiter"])
    else:
        media_use_tids.append(media_use_tid_value)

    for media_use_tid in media_use_tids:
        if not value_is_numeric(media_use_tid) and media_use_tid.strip().startswith(
            "http"
        ):
            media_use_tid = get_term_id_from_uri(config, media_use_tid.strip())
        elif not value_is_numeric(media_use_tid) and not media_use_tid.strip().startswith(
            "http"
        ):
            media_use_tid = find_term_in_vocab(
                config, "islandora_media_use", media_use_tid.strip()
            )

        term_endpoint = (
            config["host"]
            + "/taxonomy/term/"
            + str(media_use_tid).strip()
            + "?_format=json"
        )
        headers = {"Content-Type": "application/json"}
        response = issue_request(config, "GET", term_endpoint, headers)
        if response.status_code == 404:
            message = (
                'Term ID "'
                + str(media_use_tid)
                + '" registered in the "additional_files" config option '
                + 'for field "'
                + additional_field_name
                + "\" is not a term ID (term doesn't exist)."
            )
            raise WorkbenchValidationException(message)
        if response.status_code == 200:
            response_body = response.json()
            if "vid" in response_body:
                if response_body["vid"][0]["target_id"] != "islandora_media_use":
                    message = (
                        'Term ID "'
                        + str(media_use_tid)
                        + '" registered in the "additional_files" config option '
                        + 'for field "'
                        + additional_field_name
                        + '" is not in the Islandora Media Use vocabulary.'
                    )
                    raise WorkbenchValidationException(message)
            if "field_external_uri" in response_body:
                if (
                    len(response_body["field_external_uri"]) > 0
                    and response_body["field_external_uri"][0]["uri"]
                    != "http://pcdm.org/use#OriginalFile"
                ):
                    # TODO: This message is never output?
                    message = (
                        'Warning: Term ID "'
                        + str(media_use_tid)
                        + '" registered in the "additional_files" config option '
                        + 'for CSV field "'
                        + additional_field_name
                        + '" will assign an Islandora Media Use term that might '
                        + "conflict with derivative media. You should temporarily disable the Context or Action that generates those derivatives."
                    )
                else:
                    # There is no field_external_uri so we can't identify the term. Provide a generic message.
                    message = (
                        'Warning: Terms registered in the "additional_files" config option '
                        + 'for CSV field "'
                        + additional_field_name
                        + '" may assign an Islandora Media Use term that will '
                        + "conflict with derivative media. You should temporarily disable the Context or Action that generates those derivatives."
                    )
                    raise WorkbenchValidationWarning(message)
