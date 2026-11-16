from typing import Union

from islandora_workbench.drupal.client import ping_node
from islandora_workbench.drupal.media import remove_media_and_file
from islandora_workbench.drupal.node import get_media_from_node, delete_node, get_nid_from_url_alias
from islandora_workbench.tasks import register_task
from islandora_workbench.tasks.base import Task
from islandora_workbench.utils import value_is_numeric


@register_task("delete")
class DeleteTask(Task):

    name = "delete"
    required_fields = ("node_id",)
    required_config_keys = ("task", "host", "username", "password")


    def general_checks(self):
        # Only default checks and checks for required config and fields for delete task.
        pass

    def check_row(self, row: Union[dict, str], row_number: int):
        # Only default checks for column count and required fields for delete task.
        pass

    def prepare_run(self):
        if self.ctx["config"]["prompt_user_before_delete_task"] is True:
            num_nodes_to_delete = self.ctx["csv"].row_count
            if self.ctx["config"]["delete_media_with_nodes"] is True:
                delete_prompt = f"You are about to delete {num_nodes_to_delete} nodes and their attached media. Continue? (y/n) "
            else:
                delete_prompt = f"You are about to delete {num_nodes_to_delete} nodes but not their attached media. Continue? (y/n) "
            delete_response = input(delete_prompt)
            if delete_response != "y":
                self.ctx["log"].info('Response to confirmation to delete was not "y", exiting.', log_only=True)
                raise SystemExit('You didn\'t respond "y", exiting.')

    def process_row(self, row: Union[dict, str], row_number: int):
        """The per-row work; each task overrides this."""
        if not value_is_numeric(row["node_id"]):
            row["node_id"] = get_nid_from_url_alias(self.ctx["config"], row["node_id"])
        if not ping_node(self.ctx["config"], row["node_id"]):
            message = f"Node {row['node_id']} not found or not accessible, skipping delete."
            self.ctx["log"].warning(message, log_only=self.ctx["config"]["progress_bar"])
            return

        # Delete the node's media first.
        if self.ctx["config"]["delete_media_with_nodes"] is True:
            media_response_body = get_media_from_node(self.ctx["config"], row["node_id"])
            for media in media_response_body:
                if "mid" in media:
                    media_id = media["mid"][0]["value"]
                    try:
                        media_delete_status_code = remove_media_and_file(
                            self.ctx["config"], media_id
                        )
                        if media_delete_status_code == 204:
                            self.ctx["log"].info(
                                f"+ Media {self.ctx['config']['host']}/media/{media_id} deleted.",
                                log_only=self.ctx["config"]["progress_bar"]
                            )
                    except Exception as e:
                        self.ctx["log"].error(
                            f"Media {self.ctx['config']['host']}/media/{media_id} not deleted: {e}",
                            log_only=self.ctx["config"]["progress_bar"]
                        )

        node_response = delete_node(self.ctx, row["node_id"])
        if node_response["status_code"] == 204:
            row_position_message = ""
            if self.ctx["config"]["show_percentage_of_csv_input_processed"] is True:
                row_position = int(row_number) / int(self.ctx["csv"].row_count) * 100
                row_position_message = f" ({int(row_position)}%)"
            self.ctx["log"].info(
                f"Node {self.ctx['config']['host']}/node/{row['node_id']} deleted{row_position_message}.",
                log_only=self.ctx["config"]["progress_bar"]
            )
        else:
            self.ctx["log"].error(
                f"Node {self.ctx['config']['host']}/node/{row['node_id']} not deleted. See log for more detail.",
            )
            self.ctx["log"].error(
                f"Node delete returned: status code ({node_response['status_code']}), response body ({node_response['response_body']})",
                log_only=True
            )

        if self.ctx["config"]["progress_bar"] is True:
            self.ctx["progress_bar"].updateAmount(row_number)