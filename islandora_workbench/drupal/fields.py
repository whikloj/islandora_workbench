"""Classes for Drupal field operations.

Support for additional field types should be added as new classes here,
with accompanying tests in field_test.py and field_values_test.py.

Note: If new field types are added to fields.py, you must also update the WorkbenchFieldFactory
class at the bottom of this file to include the new field type and its corresponding class. This is used
by workbench and workbench_utils.py to instantiate the correct class for each field type.

"""
import json
import os
from typing import Union

from cachetools import cached, LFUCache
from cachetools.keys import hashkey

from islandora_workbench.checks.validation import validate_latlong_value, validate_edtf_date, validate_link_value, \
    validate_authority_link_value, validate_media_track_value
from islandora_workbench.drupal.client import issue_request, get_term_vocab, get_term_name, ping_node, ping_term, \
    get_entity_fields, get_entity_field_config, get_entity_field_storage
from islandora_workbench.drupal.taxonomy import prepare_term_id
from islandora_workbench.utils import value_is_numeric, split_link_string, split_media_track_string, \
    split_authority_link_string, split_typed_relation_string, split_geolocation_string, truncate_csv_value, \
    deduplicate_field_values, integration_supports_field_bundle


class WorkbenchField:
    """Base class for field operations. All subclasses must implement the create(),
    update(), remove_invalid_values(), and serialize() methods. Subclasses may also
    implement the split_string() method, which is called in the common create()."""

    def create(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
    ) -> dict:
        """
        Turn a CSV field value into a Drupal JSON field value.
        Parameters
        ----------
         ctx: dict
             The context dictionary containing the configuration and other relevant information.
         field_definitions : dict
             The field definitions object defined by get_field_definitions().
         entity : dict
             The dict that will be POSTed to Drupal as JSON.
         row : dict.
             The current CSV record.
         field_name : string
             The Drupal fieldname/CSV column header.
         Returns
         -------
         dictionary
             A dictionary representing the entity that is POSTed to Drupal as JSON.
        """
        if not row[field_name]:
            return entity

        id_field = row.get(ctx["config"].get("id_field", "not_applicable"), "not_applicable")
        subvalues = self.split_string(
            ctx["config"], row[field_name]
        )  # Split string into list of values
        subvalues = self.dedupe_values(subvalues)

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if -1 < cardinality < len(subvalues):
            subvalues = subvalues[:cardinality]
            ctx["log"].warning(
                f"Adding all values in CSV field {field_name} for record {id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
            )
        entity[field_name] = subvalues

        return entity

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        """Note: this method appends incoming CSV values to existing values, replaces existing field
        values with incoming values, or deletes all values from fields, depending on whether
        config['update_mode'] is 'append', 'replace', or 'delete'. It does not replace individual
        values within fields.
        """
        """Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            entity : dict
                The dict that will be POSTed to Drupal as JSON.
            row : dict.
                The current CSV record.
            field_name : string
                The Drupal fieldname/CSV column header.
            entity_field_values : list
                List of dictionaries containing existing value(s) for field_name in the entity being updated.
            Returns
            -------
            dictionary
                A dictionary representing the entity that is PATCHed to Drupal as JSON.
        """
        raise Exception("The update method must be implemented in all subclasses.")

    @staticmethod
    def dedupe_values(values: list) -> list:
        """Removes duplicate entries from 'values'."""
        """Parameters
           ----------
            values : list
                List containing value(s) to dedupe. Members could be strings
                from CSV or dictionairies.
            Returns
            -------
            list
                A list of unique field values.
        """
        return deduplicate_field_values(values)

    def remove_invalid_values(
        self, ctx: dict, field_definitions: dict, field_name: str, values: list
    ) -> list:
        """Removes invalid entries from 'values'.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            values : list
                List containing strings split from CSV values.
            Returns
            -------
            list
                A list of valid field values.
        """
        raise Exception(
            "The remove_invalid_values method must be implemented in all subclasses."
        )

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string|None
                A string structured same as the Workbench CSV field data for this field type,
                or None if there is nothing to return.
        """
        raise Exception("The serialize method must be implemented in all subclasses.")

    def split_string(self, config: dict, value: str):
        """Splits a string into a list of values. Called in common create() and update() methods."""
        """Parameters
           ----------
            config : dict
                The configuration settings defined by workbench_config.get_config().
            value : string
                A string to split.
            Returns
            -------
            list
                A list of values.
        """
        raise Exception(
            "The split_string method must be implemented in any subclass that wishes to use it."
        )


class SimpleField(WorkbenchField):
    """Functions for handling fields with text and other "simple" Drupal field data types,
    e.g. fields that have a "{'value': 'xxx'}" structure such as plain text fields, ETDF
    fields. All functions return an "entity" dictionary that is passed to Requests' "json"
    parameter.

    Note that text fields that are "formatted" (i.e., use text formats/output filters)
    require a 'format' key in their JSON in addition to the 'value' key. Otherwise, markup
    or other text filters won't be applied when rendered.

    Note: this class assumes that the entity has the field identified in 'field_name'.
    Callers should pre-emptively confirm that. For an example, see code near the top
    of workbench.update().

    Also note: the required Drupal field 'title' is not processed by this class.
    """

    def create(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
    ) -> dict:
        """Parameters
        ----------
         ctx: dict
             The context dictionary containing the configuration and other relevant data.
         field_definitions : dict
             The field definitions object defined by get_field_definitions().
         entity : dict
             The dict that will be POSTed to Drupal as JSON.
         row : dict.
             The current CSV record.
         field_name : string
             The Drupal fieldname/CSV column header.
         Returns
         -------
         dictionary
             A dictionary represeting the entity that is POSTed to Drupal as JSON.
        """
        if not row[field_name]:
            return entity

        if field_name in ctx["config"]["field_text_format_ids"]:
            text_format = ctx["config"]["field_text_format_ids"][field_name]
        else:
            text_format = ctx["config"]["text_format_id"]

        id_field = row.get(ctx["config"].get("id_field", "not_applicable"), "not_applicable")
        field_values = []
        subvalues = str(row[field_name]).split(ctx["config"]["subdelimiter"])
        subvalues = self.remove_invalid_values(
            ctx["config"], field_definitions, field_name, subvalues
        )
        subvalues = self.dedupe_values(subvalues)

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if -1 < cardinality < len(subvalues):
            ctx["log"].warning(
                f"Adding all values in CSV field {field_name} for record {id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
            )
            subvalues = subvalues[:cardinality]
        for subvalue in subvalues:
            subvalue = truncate_csv_value(
                field_name, id_field, field_definitions[field_name], subvalue
            )
            if (
                "formatted_text" in field_definitions[field_name]
                and field_definitions[field_name]["formatted_text"] is True
            ):
                field_values.append({"value": subvalue, "format": text_format})
            else:
                if field_definitions[field_name][
                    "field_type"
                ] == "integer" and value_is_numeric(subvalue):
                    subvalue = int(subvalue)
                if field_definitions[field_name][
                    "field_type"
                ] == "float" and value_is_numeric(subvalue, allow_decimals=True):
                    subvalue = float(subvalue)
                field_values.append({"value": subvalue})
        field_values = self.dedupe_values(field_values)
        entity[field_name] = field_values

        return entity

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        """Note: this method appends incoming CSV values to existing values, replaces existing field
        values with incoming values, or deletes all values from fields, depending on whether
        config['update_mode'] is 'append', 'replace', or 'delete'. It doesn not replace individual
        values within fields.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            entity : dict
                The dict that will be POSTed to Drupal as JSON.
            row : dict.
                The current CSV record.
            field_name : string
                The Drupal fieldname/CSV column header.
            entity_field_values : list
                List of dictionaries containing existing value(s) for field_name in the entity being updated.
            Returns
            -------
            dictionary
                A dictionary representing the entity that is PATCHed to Drupal as JSON.
        """
        if ctx["config"]["update_mode"] == "delete":
            entity[field_name] = []
            return entity

        if not row[field_name]:
            return entity

        if field_name not in entity:
            entity[field_name] = []

        if field_name in ctx["config"]["field_text_format_ids"]:
            text_format = ctx["config"]["field_text_format_ids"][field_name]
        else:
            text_format = ctx["config"]["text_format_id"]

        if ctx["config"]["task"] == "update_terms":
            entity_id_field = "term_id"
        elif ctx["config"]["task"] == "update":
            entity_id_field = "node_id"
        elif ctx["config"]["task"] in ["update_media", "update_media_by_node"]:
            entity_id_field = "media_id"

        cardinality = int(field_definitions[field_name].get("cardinality", -1))

        if ctx["config"]["update_mode"] == "append":
            subvalues = str(row[field_name]).split(ctx["config"]["subdelimiter"])
            subvalues = self.dedupe_values(subvalues)
            subvalues = self.remove_invalid_values(
                ctx["config"], field_definitions, field_name, subvalues
            )
            for subvalue in subvalues:
                subvalue = truncate_csv_value(
                    field_name,
                    row[entity_id_field],
                    field_definitions[field_name],
                    subvalue,
                )
                if (
                    "formatted_text" in field_definitions[field_name]
                    and field_definitions[field_name]["formatted_text"] is True
                ):
                    entity_field_values.append(
                        {"value": subvalue, "format": text_format}
                    )
                else:
                    if field_definitions[field_name][
                        "field_type"
                    ] == "integer" and value_is_numeric(subvalue):
                        subvalue = int(subvalue)
                    if field_definitions[field_name][
                        "field_type"
                    ] == "float" and value_is_numeric(subvalue, allow_decimals=True):
                        subvalue = float(subvalue)
                    entity_field_values.append({"value": subvalue})
            entity_field_values = self.dedupe_values(entity_field_values)
            if -1 < cardinality < len(entity_field_values):
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {row[entity_id_field]} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
                entity_field_values = entity_field_values[:cardinality]
            entity[field_name] = entity_field_values

        elif ctx["config"]["update_mode"] == "replace":
            field_values = []
            subvalues = str(row[field_name]).split(ctx["config"]["subdelimiter"])
            subvalues = self.remove_invalid_values(
                ctx["config"], field_definitions, field_name, subvalues
            )
            subvalues = self.dedupe_values(subvalues)
            if -1 < cardinality < len(subvalues):
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {row[entity_id_field]} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
                subvalues = subvalues[:cardinality]
            for subvalue in subvalues:
                subvalue = truncate_csv_value(
                    field_name,
                    row[entity_id_field],
                    field_definitions[field_name],
                    subvalue,
                )
                if (
                    "formatted_text" in field_definitions[field_name]
                    and field_definitions[field_name]["formatted_text"] is True
                ):
                    field_values.append({"value": subvalue, "format": text_format})
                else:
                    if field_definitions[field_name][
                        "field_type"
                    ] == "integer" and value_is_numeric(subvalue):
                        subvalue = int(subvalue)
                    if field_definitions[field_name][
                        "field_type"
                    ] == "float" and value_is_numeric(subvalue, allow_decimals=True):
                        subvalue = float(subvalue)
                    field_values.append({"value": subvalue})
            field_values = self.dedupe_values(field_values)
            entity[field_name] = field_values

        return entity

    def remove_invalid_values(
        self, ctx: dict, field_definitions: dict, field_name: str, values: list
    ) -> list:
        """Removes invalid entries from 'values'.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration settings and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            values : list
                List containing strings split from CSV values.
            Returns
            -------
            list
                A list of valid field values.
        """
        if "field_type" not in field_definitions[field_name]:
            return values

        if field_definitions[field_name]["field_type"] == "edtf":
            valid_values = list()
            for subvalue in values:
                if validate_edtf_date(subvalue) is True:
                    valid_values.append(subvalue)
                else:
                    message = (
                        'Value "'
                        + subvalue
                        + '" in field "'
                        + field_name
                        + '" is not a valid EDTF field value.'
                    )
                    ctx["log"].warning(message)
            return valid_values
        elif field_definitions[field_name]["field_type"] == "integer":
            valid_values = list()
            for subvalue in values:
                if value_is_numeric(subvalue) is True:
                    valid_values.append(subvalue)
                else:
                    message = (
                        'Value "'
                        + subvalue
                        + '" in field "'
                        + field_name
                        + '" is not a valid integer field value.'
                    )
                    ctx["log"].warning(message)
            return valid_values
        elif field_definitions[field_name]["field_type"] in ["decimal", "float"]:
            valid_values = list()
            for subvalue in values:
                if value_is_numeric(subvalue, allow_decimals=True) is True:
                    valid_values.append(subvalue)
                else:
                    message = (
                        'Value "'
                        + subvalue
                        + '" in field "'
                        + field_name
                        + '" is not a valid '
                        + field_definitions[field_name]["field_type"]
                        + " field value."
                    )
                    ctx["log"].warning(message)
            return valid_values
        elif field_definitions[field_name]["field_type"] == "list_string":
            valid_values = list()
            for subvalue in values:
                if subvalue in field_definitions[field_name]["allowed_values"]:
                    valid_values.append(subvalue)
                else:
                    message = (
                        'Value "'
                        + subvalue
                        + '" in field "'
                        + field_name
                        + "\" is not in the field's list of allowed values."
                    )
                    ctx["log"].warning(message)
            return valid_values
        else:
            # For now, just return values if the field is not an EDTF field.
            return values

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration settings and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string|None
                A string structured same as the Workbench CSV field data for this field type,
                or None if there is nothing to return.
        """
        if "field_type" not in field_definitions[field_name]:
            return None

        subvalues = list()
        for subvalue in field_data:
            if "value" in subvalue:
                subvalues.append(subvalue["value"])
            else:
                ctx["log"].warning(
                    "Field data "
                    + str(field_data)
                    + ' in field "'
                    + field_name
                    + '" cannot be serialized by the SimpleField handler.'
                )
                return ""

        if len(subvalues) > 1:
            return ctx["config"]["subdelimiter"].join(subvalues)
        elif len(subvalues) == 0:
            return None
        else:
            return subvalues[0]


class GeolocationField(WorkbenchField):
    """Functions for handling fields with 'geolocation' Drupal field data type.
    All functions return an "entity" dictionary that is passed to Requests'
    "json" parameter.

    Note: this class assumes that the entity has the field identified in 'field_name'.
    Callers should pre-emptively confirm that. For an example, see code near the top
    of workbench.update().
    """

    def create(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
    ) -> dict:
        # This function is unnecessary, but included for clarity. Would inherit from WorkbenchField.create() if not defined here.
        return super().create(ctx, field_definitions, entity, row, field_name)

    def split_string(self, config: dict, value: str) -> list:
        return split_geolocation_string(config, value)

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        """Note: this method appends incoming CSV values to existing values, replaces existing field
        values with incoming values, or deletes all values from fields, depending on whether
        config['update_mode'] is 'append', 'replace', or 'delete'. It does not replace individual
        values within fields.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration settings and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            entity : dict
                The dict that will be POSTed to Drupal as JSON.
            row : dict.
                The current CSV record.
            field_name : string
                The Drupal fieldname/CSV column header.
            entity_field_values : list
                List of dictionaries containing existing value(s) for field_name in the entity being updated.
            Returns
            -------
            dictionary
                A dictionary representing the entity that is PATCHed to Drupal as JSON.
        """
        if ctx["config"]["update_mode"] == "delete":
            entity[field_name] = []
            return entity

        if not row[field_name]:
            return entity

        if field_name not in entity:
            entity[field_name] = []

        if ctx["config"]["task"] == "update_terms":
            entity_id_field = "term_id"
        elif ctx["config"]["task"] == "update":
            entity_id_field = "node_id"
        elif ctx["config"]["task"] == "update_media":
            entity_id_field = "media_id"

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if ctx["config"]["update_mode"] == "replace":
            subvalues = self.split_string(ctx["config"], row[field_name])
            subvalues = self.dedupe_values(subvalues)
            field_values = []
            for subvalue in subvalues:
                field_values.append(subvalue)
            if -1 < cardinality < len(field_values):
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
                field_values = field_values[:cardinality]
            entity[field_name] = field_values

        elif ctx["config"]["update_mode"] == "append":
            subvalues = self.split_string(ctx["config"], row[field_name])
            subvalues = self.dedupe_values(subvalues)
            for subvalue in subvalues:
                entity_field_values.append(subvalue)
            if -1 < cardinality < len(entity_field_values):
                entity_field_values = entity_field_values[:cardinality]
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
            entity[field_name] = entity_field_values

        entity[field_name] = self.dedupe_values(entity[field_name])
        return entity

    def remove_invalid_values(
        self, ctx: dict, field_definitions: dict, field_name: str, values: list
    ) -> list:
        """Removes invalid entries from 'values'.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and logging information.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            values : list
                List containing strings split from CSV values.
            Returns
            -------
            list
                A list of valid field values.
        """
        valid_values = list()
        for subvalue in values:
            if validate_latlong_value(subvalue) is True:
                valid_values.append(subvalue)
            else:
                message = (
                    'Value "'
                    + subvalue
                    + '" in field "'
                    + field_name
                    + '" is not a valid Geolocation field value.'
                )
                ctx["log"].warning(message)
        return valid_values

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string|None
                A string structured same as the Workbench CSV field data for this field type,
                or None if there is nothing to return.
        """
        if "field_type" not in field_definitions[field_name]:
            return None

        subvalues = list()
        for subvalue in field_data:
            subvalues.append(str(subvalue["lat"]) + "," + str(subvalue["lng"]))

        if len(subvalues) > 1:
            return ctx["config"]["subdelimiter"].join(subvalues)
        elif len(subvalues) == 0:
            return None
        else:
            return subvalues[0]


class LinkField(WorkbenchField):
    """Functions for handling fields with 'link' Drupal field data type.
    All functions return an "entity" dictionary that is passed to Requests'
    "json" parameter.

    Note: this class assumes that the entity has the field identified in 'field_name'.
    Callers should pre-emptively confirm that. For an example, see code near the top
    of workbench.update().
    """

    def create(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
    ) -> dict:
        return super().create(ctx, field_definitions, entity, row, field_name)

    def split_string(self, config: dict, value: str) -> list:
        return split_link_string(config, value)

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        """Note: this method appends incoming CSV values to existing values, replaces existing field
        values with incoming values, or deletes all values from fields, depending on whether
        config['update_mode'] is 'append', 'replace', or 'delete'. It does not replace individual
        values within fields.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            entity : dict
                The dict that will be POSTed to Drupal as JSON.
            row : dict.
                The current CSV record.
            field_name : string
                The Drupal fieldname/CSV column header.
            entity_field_values : list
                List of dictionaries containing existing value(s) for field_name in the entity being updated.
            Returns
            -------
            dictionary
                A dictionary representing the entity that is PATCHed to Drupal as JSON.
        """
        if ctx["config"]["update_mode"] == "delete":
            entity[field_name] = []
            return entity

        if not row[field_name]:
            return entity

        if field_name not in entity:
            entity[field_name] = []

        if ctx["config"]["task"] == "update_terms":
            entity_id_field = "term_id"
        elif ctx["config"]["task"] == "update":
            entity_id_field = "node_id"
        elif ctx["config"]["task"] == "update_media":
            entity_id_field = "media_id"

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if ctx["config"]["update_mode"] == "replace":
            field_values = []
            subvalues = self.split_string(ctx["config"], row[field_name])
            subvalues = self.dedupe_values(subvalues)
            if -1 < cardinality < len(subvalues):
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
                subvalues = subvalues[:cardinality]
            for subvalue in subvalues:
                field_values.append(subvalue)
            entity[field_name] = field_values
        elif ctx["config"]["update_mode"] == "append":
            subvalues = self.split_string(ctx["config"], row[field_name])
            subvalues = self.dedupe_values(subvalues)
            for subvalue in subvalues:
                entity_field_values.append(subvalue)
            if -1 < cardinality < len(entity_field_values):
                entity_field_values = entity_field_values[:cardinality]
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
            entity[field_name] = entity_field_values

        return entity

    def remove_invalid_values(
        self, ctx: dict, field_definitions: dict, field_name: str, values: list
    ) -> list:
        """Removes invalid entries from 'values'.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            values : list
                List containing strings split from CSV values.
            Returns
            -------
            list
                A list of valid field values.
        """
        valid_values = list()
        for subvalue in values:
            if validate_link_value(subvalue) is True:
                valid_values.append(subvalue)
            else:
                message = (
                    'Value "'
                    + subvalue
                    + '" in field "'
                    + field_name
                    + '" is not a valid Link field value.'
                )
                ctx["log"].warning(message)
        return valid_values

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string|None
                A string structured same as the Workbench CSV field data for this field type.
                or None if there is nothing to return.
        """
        if "field_type" not in field_definitions[field_name]:
            return None

        subvalues = list()
        for subvalue in field_data:
            if (
                "title" in subvalue
                and subvalue["title"] is not None
                and subvalue["title"] != ""
            ):
                subvalues.append(subvalue["uri"] + "%%" + subvalue["title"])
            else:
                subvalues.append(subvalue["uri"])

        if len(subvalues) > 1:
            return ctx["config"]["subdelimiter"].join(subvalues)
        elif len(subvalues) == 0:
            return None
        else:
            return subvalues[0]


class EntityReferenceField(WorkbenchField):
    """Functions for handling fields with 'entity_reference' Drupal field data type.
    All functions return a "entity" dictionary that is passed to Requests' "json"
    parameter.

    Note: this class assumes that the entity has the field identified in 'field_name'.
    Callers should pre-emptively confirm that. For an example, see code near the top
    of workbench.update().
    """

    def create(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
    ) -> dict:
        """Parameters
        ----------
         ctx : dict
             The context dictionary containing configuration and other relevant data.
         field_definitions : dict
             The field definitions object defined by get_field_definitions().
         entity : dict
             The dict that will be POSTed to Drupal as JSON.
         row : dict.
             The current CSV record.
         field_name : string
             The Drupal fieldname/CSV column header.
         Returns
         -------
         dictionary
             A dictionary representing the entity that is POSTed to Drupal as JSON.
        """
        if not row[field_name]:
            return entity

        id_field = row.get(ctx["config"].get("id_field", "not_applicable"), "not_applicable")
        if field_definitions[field_name]["target_type"] == "taxonomy_term":
            target_type = "taxonomy_term"
            field_vocabs = field_definitions[field_name]["vocabularies"] if "vocabularies" in field_definitions[
                field_name] else False
            prepared_tids = []
            delimited_values = str(row[field_name]).split(ctx["config"]["subdelimiter"])
            for delimited_value in delimited_values:
                tid = prepare_term_id(ctx["config"], field_vocabs, field_name, delimited_value)
                if value_is_numeric(tid):
                    tid = str(tid)
                    prepared_tids.append(tid)
                else:
                    continue
            row[field_name] = ctx["config"]["subdelimiter"].join(prepared_tids)

        if field_definitions[field_name]["target_type"] == "node":
            target_type = "node_type"

        elif field_definitions[field_name]["target_type"] == "media":
            target_type = "media_type"

        elif field_definitions[field_name]["target_type"] == "domain":
            target_type = "domain"

        field_values = []
        subvalues = str(row[field_name]).split(ctx["config"]["subdelimiter"])
        subvalues = self.dedupe_values(subvalues)
        for subvalue in subvalues:
            subvalue = str(subvalue)
            field_values.append({"target_id": subvalue, "target_type": target_type})

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if -1 < cardinality < len(field_values):
            entity[field_name] = field_values[:cardinality]
            ctx["log"].warning(
                f"Adding all values in CSV field {field_name} for record {id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
            )
        else:
            entity[field_name] = field_values

        return entity

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        """Note: this method appends incoming CSV values to existing values, replaces existing field
        values with incoming values, or deletes all values from fields, depending on whether
        config['update_mode'] is 'append', 'replace', or 'delete'. It doesn not replace individual
        values within fields.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration settings and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            entity : dict
                The dict that will be POSTed to Drupal as JSON.
            row : dict.
                The current CSV record.
            field_name : string
                The Drupal fieldname/CSV column header.
            entity_field_values : list
                List of dictionaries containing existing value(s) for field_name in the entity being updated.
            Returns
            -------
            dictionary
                A dictionary representing the entity that is PATCHed to Drupal as JSON.
        """
        if ctx["config"]["update_mode"] == "delete":
            entity[field_name] = []
            return entity

        if not row[field_name]:
            return entity

        if field_name not in entity:
            entity[field_name] = []

        if ctx["config"]["task"] == "update_terms":
            entity_id_field = "term_id"
        elif ctx["config"]["task"] == "update":
            entity_id_field = "node_id"
        elif ctx["config"]["task"] == "update_media":
            entity_id_field = "media_id"

        if field_definitions[field_name]["target_type"] == "taxonomy_term":
            target_type = "taxonomy_term"
            field_vocabs = field_definitions[field_name]["vocabularies"] if "vocabularies" in field_definitions[
                field_name] else False
            prepared_tids = []
            delimited_values = str(row[field_name]).split(ctx["config"]["subdelimiter"])
            for delimited_value in delimited_values:
                tid = prepare_term_id(ctx["config"], field_vocabs, field_name, delimited_value)
                if value_is_numeric(tid):
                    tid = str(tid)
                    prepared_tids.append(tid)
                else:
                    continue
            row[field_name] = ctx["config"]["subdelimiter"].join(prepared_tids)

        if field_definitions[field_name]["target_type"] == "node":
            target_type = "node_type"

        elif field_definitions[field_name]["target_type"] == "media":
            target_type = "media_type"

        elif field_definitions[field_name]["target_type"] == "domain":
            target_type = "domain"

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if ctx["config"]["update_mode"] == "replace":
            field_values = []
            subvalues = str(row[field_name]).split(ctx["config"]["subdelimiter"])
            subvalues = self.dedupe_values(subvalues)
            for subvalue in subvalues:
                field_values.append(
                    {"target_id": str(subvalue), "target_type": target_type}
                )
            if -1 < cardinality < len(field_values):
                entity[field_name] = field_values[:cardinality]
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
            else:
                entity[field_name] = field_values

        elif ctx["config"]["update_mode"] == "append":
            subvalues = str(row[field_name]).split(ctx["config"]["subdelimiter"])
            for subvalue in subvalues:
                entity_field_values.append(
                    {"target_id": str(subvalue), "target_type": target_type}
                )
            entity_field_values = self.dedupe_values(entity_field_values)
            if -1 < cardinality < len(entity_field_values):
                entity[field_name] = entity_field_values[:cardinality]
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
            else:
                entity[field_name] = entity_field_values

        return entity

    def remove_invalid_values(
        self, ctx: dict, field_definitions: dict, field_name: str, values: list
    ) -> list:
        return values

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string|None
                A string structured same as the Workbench CSV field data for this field type,
                or None if there is nothing to return.
        """
        if "field_type" not in field_definitions[field_name]:
            return None

        subvalues = list()
        for subvalue in field_data:
            if "target_type" not in subvalue:
                # No target_type means the target item may have been deleted, so skip it.
                continue
            elif (
                ctx["config"]["export_csv_term_mode"] == "name"
                and subvalue["target_type"] == "taxonomy_term"
            ):
                # Output term names, with vocab IDs (aka namespaces).
                vocab_id = get_term_vocab(ctx["config"], subvalue["target_id"])
                term_name = get_term_name(ctx["config"], subvalue["target_id"])
                if vocab_id is not False and term_name is not False:
                    subvalues.append(vocab_id + ":" + term_name)
            elif subvalue["target_type"] == "taxonomy_term":
                # Output term IDs.
                if ping_term(ctx["config"], subvalue["target_id"]) is True:
                    subvalues.append(str(subvalue["target_id"]))
            elif subvalue["target_type"] == "node":
                # Output node IDs.
                if ping_node(ctx["config"], subvalue["target_id"]) is True:
                    subvalues.append(str(subvalue["target_id"]))
            else:
                # Fallback for any other entity type.
                subvalues.append(str(subvalue["target_id"]))

        if len(subvalues) > 1:
            return ctx["config"]["subdelimiter"].join(subvalues)
        elif len(subvalues) == 0:
            return None
        else:
            return subvalues[0]


class TypedRelationField(WorkbenchField):
    """Functions for handling fields with 'typed_relation' Drupal field data type.
    All functions return a "entity" dictionary that is passed to Requests' "json"
    parameter.

    Currently this field type only supports Typed Relation Taxonomies (not other
    Typed Relation entity types).

    Note: this class assumes that the entity has the field identified in 'field_name'.
    Callers should pre-emptively confirm that. For an example, see code near the top
    of workbench.update().
    """

    def create(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
    ) -> dict:
        """Parameters
        ----------
         ctx : dict
             The context dictionary containing configuration and other relevant data.
         field_definitions : dict
             The field definitions object defined by get_field_definitions().
         entity : dict
             The dict that will be POSTed to Drupal as JSON.
         row : dict.
             The current CSV record.
         field_name : string
             The Drupal fieldname/CSV column header.
         Returns
         -------
         dictionary
             A dictionary representing the entity that is POSTed to Drupal as JSON.
        """
        if not row[field_name]:
            return entity

        id_field = row.get(ctx["config"].get("id_field", "not_applicable"), "not_applicable")
        # Currently only supports Typed Relation taxonomy entities.
        if field_definitions[field_name]["target_type"] == "taxonomy_term":
            target_type = "taxonomy_term"
            field_vocabs = field_definitions[field_name]["vocabularies"] if "vocabularies" in field_definitions[field_name] else False
            field_values = []
            subvalues = split_typed_relation_string(
                ctx["config"], row[field_name], target_type
            )
            subvalues = self.dedupe_values(subvalues)
            cardinality = int(field_definitions[field_name].get("cardinality", -1))
            if -1 < cardinality < len(subvalues):
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
                subvalues = subvalues[:cardinality]
            for subvalue in subvalues:
                subvalue["target_id"] = prepare_term_id(
                    ctx["config"], field_vocabs, field_name, subvalue["target_id"]
                )
                field_values.append(subvalue)
            entity[field_name] = field_values

        return entity

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        """Note: this method appends incoming CSV values to existing values, replaces existing field
        values with incoming values, or deletes all values from fields, depending on whether
        config['update_mode'] is 'append', 'replace', or 'delete'. It doesn not replace individual
        values within fields.
        Parameters
           ----------
            ctx: dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            entity : dict
                The dict that will be POSTed to Drupal as JSON.
            row : dict.
                The current CSV record.
            field_name : string
                The Drupal fieldname/CSV column header.
            entity_field_values : list
                List of dictionaries containing existing value(s) for field_name in the entity being updated.
            Returns
            -------
            dictionary
                A dictionary representing the entity that is PATCHed to Drupal as JSON.
        """
        if ctx["config"]["update_mode"] == "delete":
            entity[field_name] = []
            return entity

        if not row[field_name]:
            return entity

        if field_name not in entity:
            entity[field_name] = []

        if ctx["config"]["task"] == "update_terms":
            entity_id_field = "term_id"
        elif ctx["config"]["task"] == "update":
            entity_id_field = "node_id"
        elif ctx["config"]["task"] == "update_media":
            entity_id_field = "media_id"

        # Currently only supports Typed Relation taxonomy entities.
        if field_definitions[field_name]["target_type"] == "taxonomy_term":
            target_type = "taxonomy_term"
            field_vocabs = field_definitions[field_name]["vocabularies"] if "vocabularies" in field_definitions[field_name] else False

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if ctx["config"]["update_mode"] == "replace":
            subvalues = split_typed_relation_string(
                ctx["config"], row[field_name], target_type
            )
            subvalues = self.dedupe_values(subvalues)
            field_values = []
            for subvalue in subvalues:
                subvalue["target_id"] = prepare_term_id(
                    ctx["config"], field_vocabs, field_name, subvalue["target_id"]
                )
                field_values.append(subvalue)
            if -1 < cardinality < len(field_values):
                field_values = field_values[:cardinality]
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
            entity[field_name] = field_values
        elif ctx["config"]["update_mode"] == "append":
            subvalues = split_typed_relation_string(
                ctx["config"], row[field_name], target_type
            )
            for subvalue in subvalues:
                subvalue["target_id"] = prepare_term_id(
                    ctx["config"], field_vocabs, field_name, subvalue["target_id"]
                )
                entity_field_values.append(subvalue)
            entity_field_values = self.dedupe_values(entity_field_values)
            if -1 < cardinality < len(entity_field_values):
                entity[field_name] = entity_field_values[:cardinality]
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
            else:
                entity[field_name] = entity_field_values

        return entity

    def remove_invalid_values(
        self, ctx: dict, field_definitions: dict, field_name: str, values: list
    ) -> list:
        return values

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string
                A string structured same as the Workbench CSV field data for this field type,
                or None if there is nothing to return.
        """
        if "field_type" not in field_definitions[field_name]:
            return None

        subvalues = list()
        for subvalue in field_data:
            if ctx["config"]["export_csv_term_mode"] == "name":
                vocab_id = get_term_vocab(ctx["config"], subvalue["target_id"])
                term_name = get_term_name(ctx["config"], subvalue["target_id"])
                subvalues.append(
                    str(subvalue["rel_type"]) + ":" + vocab_id + ":" + term_name
                )
            else:
                # Term IDs.
                subvalues.append(
                    str(subvalue["rel_type"]) + ":" + str(subvalue["target_id"])
                )

        if len(subvalues) > 1:
            return ctx["config"]["subdelimiter"].join(subvalues)
        elif len(subvalues) == 0:
            return None
        else:
            return subvalues[0]


class AuthorityLinkField(WorkbenchField):
    """Functions for handling fields with 'authority_link' Drupal field data type.
    All functions return a "entity" dictionary that is passed to Requests' "json"
    parameter.

    Note: this class assumes that the entity has the field identified in 'field_name'.
    Callers should pre-emptively confirm that. For an example, see code near the top
    of workbench.update().
    """

    def create(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
    ) -> dict:
        # This function is unnecessary, but is included for clarity.
        return super().create(ctx, field_definitions, entity, row, field_name)

    def split_string(self, config: dict, value: str) -> list:
        return split_authority_link_string(config, value)

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        """Note: this method appends incoming CSV values to existing values, replaces existing field
        values with incoming values, or deletes all values from fields, depending on whether
        config['update_mode'] is 'append', 'replace', or 'delete'. It does not replace individual
        values within fields.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            entity : dict
                The dict that will be POSTed to Drupal as JSON.
            row : dict.
                The current CSV record.
            field_name : string
                The Drupal fieldname/CSV column header.
            entity_field_values : list
                List of dictionaries containing existing value(s) for field_name in the entity being updated.
            Returns
            -------
            dictionary
                A dictionary representing the entity that is PATCHed to Drupal as JSON.
        """
        if ctx["config"]["update_mode"] == "delete":
            entity[field_name] = []
            return entity

        if not row[field_name]:
            return entity

        if field_name not in entity:
            entity[field_name] = []

        if ctx["config"]["task"] == "update_terms":
            entity_id_field = "term_id"
        elif ctx["config"]["task"] == "update":
            entity_id_field = "node_id"
        elif ctx["config"]["task"] == "update_media":
            entity_id_field = "media_id"

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if ctx["config"]["update_mode"] == "replace":
            field_values = []
            subvalues = self.split_string(ctx["config"], row[field_name])
            subvalues = self.dedupe_values(subvalues)
            if -1 < cardinality < len(subvalues):
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
                subvalues = subvalues[:cardinality]
            for subvalue in subvalues:
                field_values.append(subvalue)
            entity[field_name] = field_values
        elif ctx["config"]["update_mode"] == "append":
            subvalues = self.split_string(ctx["config"], row[field_name])
            for subvalue in subvalues:
                entity_field_values.append(subvalue)
            if -1 < cardinality < len(entity_field_values):
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {entity_id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
                entity[field_name] = entity_field_values[:cardinality]
            else:
                entity[field_name] = entity_field_values

        return entity

    def remove_invalid_values(
        self, ctx: dict, field_definitions: dict, field_name: str, values: list
    ) -> list:
        """Removes invalid entries from 'values'.
        Parameters
           ----------
            ctx : dict
                The context object containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            values : list
                List containing strings split from CSV values.
            Returns
            -------
            list
                A list of valid field values.
        """
        valid_values = list()
        for subvalue in values:
            if (
                validate_authority_link_value(
                    subvalue, field_definitions[field_name]["authority_sources"]
                )
                is True
            ):
                valid_values.append(subvalue)
            else:
                ctx["log"].warning(
                    f'Value "{subvalue}" in field "{field_name}" is not a valid Authority Link field value.',
                    log_only=True
                )
        return valid_values

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The context object containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string|None
                A string structured same as the Workbench CSV field data for this field type,
                or None if there is nothing to return.
        """
        if "field_type" not in field_definitions[field_name]:
            return None

        subvalues = list()
        for subvalue in field_data:
            if "title" in subvalue and subvalue["title"] is not None:
                subvalues.append(
                    subvalue["source"]
                    + "%%"
                    + subvalue["uri"]
                    + "%%"
                    + subvalue["title"]
                )
            else:
                subvalues.append(subvalue["source"] + "%%" + subvalue["uri"])

        if len(subvalues) > 1:
            return ctx["config"]["subdelimiter"].join(subvalues)
        elif len(subvalues) == 0:
            return None
        else:
            return subvalues[0]


class MediaTrackField(WorkbenchField):
    """Functions for handling fields with "media_track" Drupal (Islandora) field data type.
    All functions return a "entity" dictionary that is passed to Requests' "json"
    parameter.

    Note: this class assumes that the entity has the field identified in "field_name".
    Callers should pre-emptively confirm that. For an example, see code near the top
    of workbench.update().
    """

    def create(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
    ) -> dict:
        # This function is unnecessary, but is included for clarity.
        return super().create(ctx, field_definitions, entity, row, field_name)

    def split_string(self, config: dict, value: str) -> list:
        return split_media_track_string(config, value)

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        if ctx["config"]["update_mode"] == "delete":
            entity[field_name] = []
            return entity

        if not row[field_name]:
            return entity

        if field_name not in entity:
            entity[field_name] = []

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if ctx["config"]["update_mode"] == "replace":
            field_values = []
            subvalues = self.split_string(ctx["config"], row[field_name])
            subvalues = self.dedupe_values(subvalues)
            if -1 < cardinality < len(subvalues):
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {row['node_id']} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
                subvalues = subvalues[:cardinality]
            for subvalue in subvalues:
                field_values.append(subvalue)
            entity[field_name] = field_values
        elif ctx["config"]["update_mode"] == "append":
            subvalues = self.split_string(ctx["config"], row[field_name])
            for subvalue in subvalues:
                entity_field_values.append(subvalue)
            if -1 < cardinality < len(entity_field_values):
                entity[field_name] = entity_field_values[:cardinality]
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {row['node_id']} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
            else:
                entity[field_name] = entity_field_values
        return entity

    def remove_invalid_values(
        self, ctx: dict, field_definitions: dict, field_name: str, values: list
    ) -> list:
        """Removes invalid entries from 'values'.
        Parameters
           ----------
            ctx : dict
                The context object containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            values : list
                List containing strings split from CSV values.
            Returns
            -------
            list
                A list of valid field values.
        """
        valid_values = list()
        for subvalue in values:
            if (
                validate_media_track_value(
                    subvalue
                )
                is True
            ):
                valid_values.append(subvalue)
            else:
                message = (
                    'Value "'
                    + subvalue
                    + '" in field "'
                    + field_name
                    + '" is not a valid Authority Link field value.'
                )
                ctx["log"].warning(message)
        return valid_values

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The configuration settings defined by workbench_config.get_config().
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string|None
                A string structured same as the Workbench CSV field data for this field type,
                or None if there is nothing to return.
        """
        if "field_type" not in field_definitions[field_name]:
            return None

        subvalues = list()
        for subvalue in field_data:
            if all(
                "label" in subvalue,
                subvalue["label"] is not None,
                "kind" in subvalue,
                subvalue["kind"] is not None,
                "srclang" in subvalue,
                subvalue["srclang"] is not None,
                "url" in subvalue,
                subvalue["url"] is not None,
            ):
                serialized = f"{subvalue['label']}:{subvalue['kind']}:{subvalue['srclang']}:{os.path.basename(subvalue['url'])}"
                subvalues.append(serialized)
            else:
                subvalues.append(
                    f"{subvalue['label']}:{subvalue['kind']}:{subvalue['srclang']}:{os.path.basename(subvalue['url'])}"
                )

        if len(subvalues) > 1:
            return ctx["config"]["subdelimiter"].join(subvalues)
        elif len(subvalues) == 0:
            return None
        else:
            return subvalues[0]


class EntityReferenceRevisionsField(WorkbenchField):
    """Functions for handling fields with 'entity_reference_revisions' Drupal field
    data type. This field *can* reference nodes, taxonomy terms, and media, but
    workbench only supports paragraphs for now.

    All functions return an "entity" dictionary that is passed to Requests' "json"
    parameter.
    """

    paragraph_field_definitions = {}

    def create(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
    ) -> dict:
        """Parameters
        ----------
         ctx : dict
             The context object containing configuration and other relevant data.
         field_definitions : dict
             The field definitions object defined by get_field_definitions().
         entity : dict
             The dict that will be POSTed to Drupal as JSON.
         row : dict.
             The current CSV record.
         field_name : string
             The Drupal fieldname/CSV column header.
         Returns
         -------
         dictionary
             A dictionary represeting the entity that is POSTed to Drupal as JSON.
        """
        if not row[field_name]:
            ctx["log"].warning(f'Did not find "{field_name}" in row.')
            return entity
        id_field = row.get(ctx["config"].get("id_field", "not_applicable"), "not_applicable")

        # This field *can* reference nodes, taxonomy terms, and media, but workbench
        # only supports paragraphs for now.
        if not field_definitions[field_name]["target_type"] == "paragraph":
            return entity

        # We allow fields to overide the global subdelimiter.
        paragraph_configs = (
            ctx["config"].get("paragraph_fields", {})
            .get(field_definitions[field_name]["entity_type"], {})
            .get(field_name, {})
        )
        subdelimiter = (
            paragraph_configs.get("subdelimiter", None) or ctx["config"]["subdelimiter"]
        )

        subvalues = str(row[field_name]).split(subdelimiter)

        # @todo self.dedup_values

        # Enforce cardinality.
        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        if -1 < cardinality < len(subvalues):
            ctx["log"].warning(
                f"Adding all values in CSV field {field_name} for record {id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
            )
            subvalues = subvalues[:cardinality]

        # Paragraphs are essentially field bundles, like any other entity,
        # the difference is that this node "owns" the entity rather
        # than simply references it. So we need to parse the row data
        # and populate the paragraph entity, which looks a lot like
        # creating a node from the CSV row...

        # Cache paragraph field definitions
        paragraph_type = paragraph_configs.get("type")
        if not paragraph_type:
            ctx["log"].warning(
                f'Could not determine target paragraph type for field "{field_name}"'
            )
            return entity

        if not self.paragraph_field_definitions.get(paragraph_type):
            self.paragraph_field_definitions[paragraph_type] = get_field_definitions(
                ctx["config"], "paragraph", paragraph_type
            )

        reference_revisions = []
        for subvalue in subvalues:
            # Zip together the fields and their values.
            paragraph = dict(
                zip(
                    paragraph_configs.get("field_order", {}),
                    subvalue.split(paragraph_configs.get("field_delimiter", ":")),
                )
            )
            paragraph_ordered = dict(paragraph)

            # Process each field's value.
            for p_field, value in paragraph.items():
                # This certainly isn't DRY, but here we go.

                field = WorkbenchFieldFactory.get_field_handler(self.paragraph_field_definitions[paragraph_type].get(p_field))
                paragraph = field.create(
                    ctx["config"],
                    self.paragraph_field_definitions[paragraph_type],
                    paragraph,
                    paragraph_ordered,
                    p_field,
                )

            # Set parent information.
            paragraph.update(
                {
                    "type": [{"target_id": paragraph_configs.get("type")}],
                    "parent_field_name": [{"value": field_name}],
                }
            )

            # Create the paragraph.
            p_response = issue_request(
                ctx["config"],
                "POST",
                "/entity/paragraph?_format=json",
                {"Content-Type": "application/json"},
                paragraph,
                None,
            )
            if p_response.status_code == 201:
                paragraph = p_response.json()
                reference_revisions.append(
                    {
                        "target_id": paragraph["id"][0]["value"],
                        "target_revision_id": paragraph["revision_id"][0]["value"],
                    }
                )
            elif p_response.status_code == 403:
                message = "Not authorized to create paragraphs. Please ensure the paragraphs_type_permissions module is enable and the user has sufficient permissions."
                ctx["log"].error(message)
            else:
                message = p_response.json().get("message", "Unknown")
                ctx["log"].warn(
                    f'Could not create paragraph for "{field_name}" in row "{id_field}": {message}'
                )

        entity[field_name] = reference_revisions
        return entity

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        """Note: this method appends incoming CSV values to existing values, replaces existing field
        values with incoming values, or deletes all values from fields, depending on whether
        config['update_mode'] is 'append', 'replace', or 'delete'. It doesn not replace individual
        values within fields.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing the configuration and other relevant information.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            entity : dict
                The dict that will be POSTed to Drupal as JSON.
            row : dict.
                The current CSV record.
            field_name : string
                The Drupal fieldname/CSV column header.
            entity_field_values : list
                List of dictionaries containing existing value(s) for field_name in the entity being updated.
            Returns
            -------
            dictionary
                A dictionary representing the entity that is PATCHed to Drupal as JSON.
        """
        if ctx["config"]["update_mode"] == "delete":
            entity[field_name] = []
            return entity

        if not row[field_name]:
            return entity

        if field_name not in entity:
            entity[field_name] = []

        if ctx["config"]["update_mode"] == "replace":
            return self.create(ctx, field_definitions, entity, row, field_name)

        elif ctx["config"]["update_mode"] == "append":
            # Save away existing values
            entity = self.create(ctx, field_definitions, entity, row, field_name)
            entity[field_name] = entity_field_values + entity[field_name]
            # Enforce cardinality
            cardinality = int(field_definitions[field_name].get("cardinality", -1))
            if -1 < cardinality < len(entity[field_name]):
                id_field = row.get(ctx["config"]["id_field"], "not_applicable")
                ctx["log"].warning(
                    f"Adding all values in CSV field {field_name} for record {id_field} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
                )
                entity[field_name] = entity[field_name][slice(0, cardinality)]
            return entity

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string|None
                A string structured same as the Workbench CSV field data for this field type,
                or None if there is nothing to return.
        """
        if "field_type" not in field_definitions[field_name]:
            return None

        # We allow fields to overide the global subdelimiter.
        paragraph_configs = (
            ctx["config"].get("paragraph_fields", {})
            .get(field_definitions[field_name]["entity_type"], {})
            .get(field_name, {})
        )
        subdelimiter = (
            paragraph_configs.get("subdelimiter", None) or ctx["config"]["subdelimiter"]
        )

        # Cache paragraph field definitions
        paragraph_type = paragraph_configs.get("type")
        if not paragraph_type:
            ctx["log"].warn(
                f'Could not determine target paragraph type for field "field_name". Returning data from Drupal.'
            )
            return json.dumps(field_data)
        if not self.paragraph_field_definitions.get(paragraph_type):
            self.paragraph_field_definitions[paragraph_type] = get_field_definitions(
                ctx["config"], "paragraph", paragraph_type
            )

        subvalues = list()
        for subvalue in field_data:
            # Retrieve the paragraph so we can serialize it.
            target_id = subvalue.get("target_id")
            p_response = issue_request(
                ctx["config"], "GET", f"/entity/paragraph/{target_id}?_format=json"
            )
            if p_response.status_code == 200:
                paragraph = p_response.json()
                paragraph_parts = []
                for field in paragraph_configs.get("field_order", {}):
                    ctx["log"].info(
                        f"Serializing paragraph field: {field}:"
                        + json.dumps(paragraph.get(field))
                    )
                    if not paragraph.get(field):
                        continue

                    serialized_field = WorkbenchFieldFactory.get_field_handler(
                        self.paragraph_field_definitions[paragraph_type][field][
                            "field_type"
                        ]
                    )
                    paragraph_parts.append(
                        serialized_field.serialize(
                            ctx["config"],
                            self.paragraph_field_definitions[paragraph_type],
                            field,
                            paragraph.get(field),
                        )
                    )
                subvalues.append(
                    paragraph_configs.get("field_delimiter", ":").join(paragraph_parts)
                )
            else:
                # Something went wrong, so we'll just return the Drupal field data we already have.
                message = p_response.json().get("message", "Unknown")
                ctx["log"].warn(
                    f'Could not retrieve paragraph for "{field_name}": {message}'
                )
                subvalues.append(subvalue)
        if len(subvalues) > 1:
            return subdelimiter.join(subvalues)
        elif len(subvalues) == 0:
            return None
        return subvalues[0]


class LinkedDataField(LinkField):
    """Linked Data Field is very similar to a Link field except it has a different JSON dictionary keys serialization"""

    def update(
        self,
        ctx: dict,
        field_definitions: dict,
        entity: dict,
        row: dict,
        field_name: str,
        entity_field_values: list,
    ) -> dict:
        """Note: this method appends incoming CSV values to existing values, replaces existing field
        values with incoming values, or deletes all values from fields, depending on whether
        config['update_mode'] is 'append', 'replace', or 'delete'. It does not replace individual
        values within fields.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            entity : dict
                The dict that will be POSTed to Drupal as JSON.
            row : dict.
                The current CSV record.
            field_name : string
                The Drupal fieldname/CSV column header.
            entity_field_values : list
                List of dictionaries containing existing value(s) for field_name in the entity being updated.
            Returns
            -------
            dictionary
                A dictionary representing the entity that is PATCHed to Drupal as JSON.
        """
        if ctx["config"]["update_mode"] == "delete":
            entity[field_name] = []
            return entity

        if not row[field_name]:
            return entity

        if field_name not in entity:
            entity[field_name] = []

        if ctx["config"]["task"] == "update":
            entity_id_field = "node_id"
        else:
            return entity

        if ctx["config"]["update_mode"] not in ["append", "replace"]:
            return entity

        cardinality = int(field_definitions[field_name].get("cardinality", -1))
        subvalues = []
        if ctx["config"]["update_mode"] == "append":
            # For append add existing values, then new values and then dedupe and replace
            subvalues.extend(entity_field_values)
        subvalues.extend(self.split_string(ctx["config"], row[field_name]))
        subvalues = self.dedupe_values(subvalues)

        if -1 < cardinality < len(subvalues):
            ctx["log"].warning(
                f"Adding all values in CSV field {field_name} for record {row[entity_id_field]} would exceed maximum number of allowed values ({cardinality}). Skipping adding extra values."
            )
            subvalues = subvalues[:cardinality]
        entity[field_name] = subvalues

        return entity

    def serialize(
        self, ctx: dict, field_definitions: dict, field_name: str, field_data: str
    ) -> Union[str, None]:
        """Serialized values into a format consistent with Workbench's CSV-field input format.
        Parameters
           ----------
            ctx : dict
                The context dictionary containing the configuration and other relevant data.
            field_definitions : dict
                The field definitions object defined by get_field_definitions().
            field_name : string
                The Drupal fieldname/CSV column header.
            field_data : string
                Raw JSON from the field named 'field_name'.
            Returns
            -------
            string|None
                A string structured same as the Workbench CSV field data for this field type.
                or None if there is nothing to return.
        """
        if "field_type" not in field_definitions[field_name]:
            return None

        subvalues = list()
        for subvalue in field_data:
            if (
                "value" in subvalue
                and subvalue["value"] is not None
                and subvalue["value"] != ""
            ):
                subvalues.append(subvalue["url"] + "%%" + subvalue["value"])
            else:
                subvalues.append(subvalue["url"])

        if len(subvalues) > 1:
            return ctx["config"]["subdelimiter"].join(subvalues)
        elif len(subvalues) == 0:
            return None
        else:
            return subvalues[0]

    def split_string(self, config: dict, value: str) -> list:
        """Linked Data fields have different keys."""
        return_list = split_link_string(config, value)
        new_list = [{"url": d["uri"], "value": d["title"]} for d in return_list]
        return new_list


class WorkbenchFieldFactory:

    @staticmethod
    def get_field_handler(field_type: str) -> WorkbenchField:
        """Returns a WorkbenchField subclass based on the field type."""
        """Parameters
        field_type : string
            The Drupal field type.
        Returns
        -------
        WorkbenchField
            A WorkbenchField subclass.
        """
        field_to_class_map = {
            "entity_reference": "EntityReferenceField",
            "entity_reference_revisions": "EntityReferenceRevisionsField",
            "typed_relation": "TypedRelationField",
            "authority_link": "AuthorityLinkField",
            "media_track": "MediaTrackField",
            "geolocation": "GeolocationField",
            "link": "LinkField",
            "linked_data_field": "LinkedDataField",
        }
        """Returns a WorkbenchField subclass based on the field type."""
        if field_type in field_to_class_map:
            return globals()[field_to_class_map[field_type]]()
        else:
            return SimpleField()


def serialize_field_json(
    config: dict, field_definitions: dict, field_name: str, field_data: str
) -> str:
    """Serializes JSON from a Drupal field into a string consistent with Workbench's CSV-field input format.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        field_definitions : dict
            The field definitions object defined by get_field_definitions().
        field_name : string
            The Drupal fieldname/CSV column header.
        field_data : string
            Raw JSON from the field named 'field_name'.
        Returns
        -------
        string
            A string structured same as the Workbench CSV field data for the field type.
    """
    # Assemble CSV output Drupal field data. If new field types are added to
    # fields.py, they need to be registered in the following if/elif/else block.

    serialized_field = WorkbenchFieldFactory.get_field_handler(
        field_definitions[field_name]["field_type"]
    )
    csv_field_data = serialized_field.serialize(
        config, field_definitions, field_name, field_data
    )

    return csv_field_data


def parse_field_definition(
    config: dict, fields: dict, entity_type: str, bundle_type: str
) -> dict:
    """Parse a single field's definition into a more digestible format.
    Parameters
    :param config: dict - The configuration settings defined by workbench_config.get_config().
    :param fields: dict - The field names and information or just names. From get_entity_fields().
    :param entity_type: string - One of 'node', 'media', 'taxonomy_term', or 'paragraph'.
    :param bundle_type: string - The bundle type of the entity.
    :return: dict - A dictionary with the field's properties in a more digestible format.
    """
    field_definitions = {}
    for fieldname, field_info in fields.items():
        if integration_supports_field_bundle(config):
            field_config = field_info["config"]
            field_storage_config = field_info["storage_config"]
        else:
            raw_field_config = get_entity_field_config(
                config, fieldname, entity_type, bundle_type
            )
            field_config = json.loads(raw_field_config)
            raw_field_storage = get_entity_field_storage(config, fieldname, entity_type)
            field_storage_config = json.loads(raw_field_storage)
        # Currently skip all base fields as they screw up required field checks.
        if (
            "is_base_field" in field_config.keys()
            and field_config["is_base_field"] is True
        ):
            continue
        field_definitions[fieldname] = {}
        field_definition = {"entity_type": field_config["entity_type"]}

        if field_config["entity_type"] == "media":
            field_definition["media_type"] = field_config["bundle"]

        field_definition["required"] = field_config["required"]
        field_definition["label"] = field_config["label"]

        if "dependencies" in field_config and "config" in field_config["dependencies"]:
            # Base fields tend to lack the dependencies key.
            vocabularies = [
                x.replace("taxonomy.vocabulary.", "")
                for x in field_config["dependencies"]["config"]
                if x.startswith("taxonomy.vocabulary.")
            ]
            if len(vocabularies) > 0:
                field_definition["vocabularies"] = vocabularies

        # Reference 'handler' could be nothing, 'default:taxonomy_term' (or some other entity type), or 'views'.
        if "handler" in field_config["settings"]:
            field_definition["handler"] = field_config["settings"]["handler"]
        else:
            field_definition["handler"] = None

        if "handler_settings" in field_config["settings"]:
            field_definition["handler_settings"] = field_config["settings"][
                "handler_settings"
            ]
        else:
            field_definition["handler_settings"] = None

        if entity_type == "media":
            if "file_extensions" in field_config["settings"]:
                field_definition["file_extensions"] = field_config["settings"][
                    "file_extensions"
                ]

        field_definition["field_type"] = field_storage_config["type"]
        field_definition["cardinality"] = field_storage_config["cardinality"]

        if "max_length" in field_storage_config["settings"]:
            field_definition["max_length"] = field_storage_config["settings"][
                "max_length"
            ]
        else:
            field_definition["max_length"] = None

        if "target_type" in field_storage_config["settings"]:
            field_definition["target_type"] = field_storage_config["settings"][
                "target_type"
            ]
        else:
            field_definition["target_type"] = None

        if (
            field_storage_config["type"] == "typed_relation"
            and "rel_types" in field_config["settings"]
        ):
            field_definition["typed_relations"] = field_config["settings"]["rel_types"]

        if "authority_sources" in field_config["settings"]:
            field_definition["authority_sources"] = list(
                field_config["settings"]["authority_sources"].keys()
            )
        else:
            field_definition["authority_sources"] = None

        if "allowed_values" in field_storage_config["settings"]:
            field_definition["allowed_values"] = list(
                field_storage_config["settings"]["allowed_values"].keys()
            )
        else:
            field_definition["allowed_values"] = None

        if field_config["field_type"].startswith("text"):
            field_definition["formatted_text"] = True
        else:
            field_definition["formatted_text"] = False
        field_definitions[fieldname] = field_definition

    return field_definitions


def cache_key(*args):
    """Custom cache key function for get_field_definitions.
    We want to cache field definitions by entity type and bundle, but because the bundle_type is not required means we
    need to handle getting the content_type config key to construct the cache key.
    Parameters
    :param args: The arguments passed to get_field_definitions, which are config, entity_type, and optionally bundle_type.
    Returns
    :return: A cache key string constructed from the entity type and bundle type (or content type for nodes).
    """
    if len(args) <= 1:
        raise ValueError(
            "cache_key function requires at least 2 arguments to construct a cache key."
        )
    elif len(args) < 3 and args[1] == "node":
        return hashkey(args[1], args[0]["content_type"])
    else:
        return hashkey(args[1], args[2])


@cached(cache=LFUCache(maxsize=128), key=cache_key)
def get_field_definitions(
    config: dict, entity_type: str, bundle_type: str = None
) -> dict:
    """Get field definitions from Drupal.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    entity_type : string
        One of 'node', 'media', 'taxonomy_term', or 'paragraph'.
    bundle_type : string, optional
        None for nodes (the content type can optionally be gotten from config),
        the vocabulary name, or the media type (image', 'document', 'audio',
        'video', 'file', etc.).
    Returns
    -------
    dict
        A dictionary with field names as keys and values arrays containing
        field config data. Config data varies slightly by entity type.
    """
    if entity_type == "node":
        if bundle_type is None:
            bundle_type = config["content_type"]
        max_length = int(config["max_node_title_length"])
        label = "Title"
        field_key = "title"

    elif entity_type == "taxonomy_term":
        max_length = 255
        label = "Name"
        field_key = "term_name"

    elif entity_type == "media":
        max_length = 255
        label = "Name"
        field_key = "name"

    fields = get_entity_fields(config, entity_type, bundle_type)
    field_definitions = parse_field_definition(
        config, fields, entity_type, bundle_type
    )

    if entity_type != "paragraph":
        # Paragraphs don't seem to need this field defined.
        field_definitions[field_key] = {
            "entity_type": entity_type,
            "required": True,
            "label": label,
            "field_type": "string",
            "cardinality": 1,
            "max_length": max_length,
            "target_type": None,
            "handler": None,
            "handler_settings": None,
        }


    return field_definitions


def get_required_bundle_fields(
    config: dict, entity_type: str, bundle_type: str
) -> list:
    """Gets a list of required fields for the given bundle type.

    Parameters
    ----------
    config : dict
        The configuration settings defined by workbench_config.get_config().
    entity_type : string
        One of 'node', 'media', or 'taxonomy_term'.
    bundle_type : string
        The (node) content type, the vocabulary name, or the media type ('image',
        'document', 'audio', 'video', 'file', etc.).

    Returns
    -------
    list
        A list of Drupal field names that are configured as required for this bundle, e.g
        ['required_field1_name', 'required_field2_name'].
    """
    field_definitions = get_field_definitions(config, entity_type, bundle_type)
    required_drupal_fields = list()
    for drupal_fieldname, field_info in field_definitions.items():
        if (
            "entity_type" in field_info
            and field_info["entity_type"] == entity_type
            and "required" in field_info
            and field_info["required"] is True
        ):
            required_drupal_fields.append(drupal_fieldname)
    return required_drupal_fields


