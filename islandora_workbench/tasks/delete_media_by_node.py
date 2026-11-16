from typing import Union

from islandora_workbench.drupal.media import get_node_media_ids
from islandora_workbench.tasks import register_task
from islandora_workbench.tasks.delete_media import DeleteMediaTask

@register_task("delete_media_by_node")
class DeleteMediaByNodeTask(DeleteMediaTask):

    name = "delete_media_by_node"
    required_fields = ("node_id",)

    def process_row(self, row: Union[dict, str], row_number: int):
        """Because delete_media_by_node has the node_id, we resolve the media_ids here."""
        node_id = row.get("node_id")
        if not node_id:
            self.ctx["log"].error(f"Row {row_number}: Missing required field 'node_id'.")
            return None

        media_ids = get_node_media_ids(
            self.ctx["config"],
            node_id,
            self.ctx["config"]["update_media_by_node_media_use_tids"],
            self.ctx["config"]["media_type"]
        )
        if not media_ids:
            self.ctx["log"].error(f"Row {row_number}: No media found for node_id '{node_id}'.")
            return None
        elif len(media_ids) == 1:
            # Set the media_id and pass to the update_media task to perform the update.
            row["media_id"] = media_ids[0]
            return super().process_row(row, row_number)
        elif len(media_ids) == 0:
            self.ctx["log"].warning(f'Row {row_number}: No matching media on node {row["node_id"]} found.')
        else:
            self.ctx["log"].info(
                f'Multiple matching media on node {row["node_id"]} found, with media IDs {", ".join([str(x) for x in media_ids]).strip()}. Workbench can only update one media per node at a time.'
            )
        return None
