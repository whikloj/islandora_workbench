from typing import Union

from islandora_workbench.drupal.client import get_mid_from_media_url_alias
from islandora_workbench.drupal.media import remove_media_and_file
from islandora_workbench.tasks import Task, register_task
from islandora_workbench.utils import value_is_numeric


@register_task("delete_media")
class DeleteMediaTask(Task):

    name = "delete_media"
    required_fields = ("media_id",)
    required_config_keys = ("task", "host", "username", "password")

    def general_checks(self):
        # Only default checks and checks for required config keys for delete_media task.
        pass

    def check_row(self, row: Union[dict, str], row_number: int):
        # Only default checks for column count and required fields for delete_media task.
        pass

    def prepare_run(self):
        pass

    def process_row(self, row: Union[dict, str], row_number: int):
        if not value_is_numeric(row["media_id"]):
            row["media_id"] = get_mid_from_media_url_alias(self.ctx["config"], row["node_id"])

        media_delete_status_code = remove_media_and_file(self.ctx["config"], row["media_id"])
        if media_delete_status_code == 204:
            message = (
                    "Media " + self.ctx["config"]["host"] + "/media/" + str(row["media_id"]) + " and associated file deleted.")
            self.ctx["log"].info(
                message,
                log_only=self.ctx["config"]["progress_bar"]
            )
        else:
            message = ("Media " + self.ctx["config"]["host"] + "/media/" + str(
                row["media_id"]
                ) + " and associated file not deleted.")
            self.ctx["log"].error(
                message + " See log for more information.",
                log_message=message + f" HTTP response code {media_delete_status_code}."
            )

        if self.ctx["config"]["progress_bar"] is True:
            self.ctx["progress_bar"].updateAmount(row_number)
