import csv
import json
from typing import Union

from islandora_workbench.drupal.fields import get_field_definitions, serialize_field_json


class OutputCSVWriter:
    """Class to handle writing to the output CSV file."""

    # Don't include these fields from each node Drupal fields in our output
    # (but we add back in type/"content_type", status/"published", and langcode below).
    FIELDS_TO_REMOVE: list[str] = ["nid", "vid", "type", "status", "langcode", "created", "changed", "default_langcode", "uid",
        "sticky", "revision_timestamp", "revision_translation_affected", "revision_uid", "revision_log",
        "content_translation_source", "content_translation_outdated", ]

    def __init__(self, ctx: dict, id_field:str, fields_to_exclude: list):
        self.ctx = ctx
        self.id_field = id_field
        self.output_csv_path = ctx["config"]["output_csv"]
        self.output_csv_include_input_csv = ctx["config"]["output_csv_include_input_csv"]
        self.fields_to_exclude = fields_to_exclude
        self.field_definitions = get_field_definitions(ctx["config"], "node")
        self.initialized = False

    def write_to_output_csv(
        self, row_id: str, node_json: Union[str, dict], input_csv_row: Union[dict, None] = None
    ):
        """Appends a row to the CSV file located at config['output_csv'].
        If config['output_csv_include_input_csv'] is true, includes values
        from the input CSV.
        Parameters
            ----------
            row_id : str
                The value of the CSV row's ID field.
            node_json : str|dict
                The JSON or a string representation of the node just created, provided by Drupal.
            input_csv_row : dict
                The CSV Reader representation of the current input CSV row. Note that
                this is copy.deepcopy() of the CSV row since passing the row in as is
                modifies it in global scope.
            Returns
            -------
            None
        """
        node_dict = node_json
        if not isinstance(node_json, dict):
            node_dict = json.loads(node_json)
        node_field_names = list(node_dict.keys())
        node_field_names.insert(0, "langcode")
        node_field_names.insert(0, "promote")
        node_field_names.insert(0, "published")
        node_field_names.insert(0, "content_type")
        node_field_names.insert(0, "node_id")
        # "id_field" needs to be the first column header for the check below.
        node_field_names.insert(0, self.id_field)

        # Don't include these input CSV fields in our output, except for "published", "promote",
        # and "langcode", which we populate separately below from node data.
        if input_csv_row is not None and self.output_csv_include_input_csv:
            input_csv_row_fieldnames = list(input_csv_row.keys())
            for field_name in input_csv_row_fieldnames:
                if field_name not in self.fields_to_exclude and field_name not in node_field_names:
                    node_field_names.append(field_name)

        for field_to_remove in self.FIELDS_TO_REMOVE:
            if field_to_remove in node_field_names:
                node_field_names.remove(field_to_remove)

        with open(self.output_csv_path, mode="a+", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=node_field_names, lineterminator="\n")
            if not self.initialized:
                writer.writeheader()
                self.initialized = True

            # Assemble the CSV record to write.
            row = dict()
            row[self.id_field] = row_id
            row["node_id"] = node_dict["nid"][0]["value"]
            row["uuid"] = node_dict["uuid"][0]["value"]
            row["title"] = node_dict["title"][0]["value"]
            row["published"] = "1" if node_dict["status"][0]["value"] is True else "0"
            row["promote"] = "1" if node_dict["promote"][0]["value"] is True else "0"
            row["content_type"] = node_dict["type"][0]["target_id"]
            row["langcode"] = node_dict["langcode"][0]["value"]

            if input_csv_row is not None and self.output_csv_include_input_csv:
                for field_name, field_value in input_csv_row.items():
                    if field_name not in self.fields_to_exclude:
                        row[field_name] = field_value
                # Then append the input row to the new node data.
                for field_name in node_dict:
                    if field_name.startswith("field_"):
                        row[field_name] = serialize_field_json(
                            self.ctx["config"], self.field_definitions, field_name, node_dict[field_name]
                        )

                row.update(input_csv_row)
            writer.writerow(row)
