import sys

from islandora_workbench.tasks.base import Task
from workbench_utils import check_for_required_config_keys, check_csv_file_exists, get_csv_data


class DeleteTask(Task):
    def __init__(self, ctx: dict):
        super().__init__(ctx)
        self.ctx = ctx

    def check(self):
        super().check()

        try:
            check_for_required_config_keys(
                self.ctx.config, ["task", "host", "username", "password"], throw_exception=True
            )
        except KeyError as e:
            self.ctx.log.error(f"Missing required configuration key: {e}")
            sys.exit(1)  # todo: replace with an exception

        message = "OK, configuration file has all required values (did not check for optional values)."
        self.ctx.log.info(message)

        check_csv_file_exists(self.ctx.config, "node_fields")

        csv_data = get_csv_data(self.ctx.config)

    def prepare(self):
        """Prepare the task for execution. (i.e. rollback files, ID map, csv load, etc.)"""
        pass

    def rows(self):
        """yields rows after start/stop filtering"""
        pass

    def process_row(self, row):
        """The per-row work; each task overrides this."""
        pass

    def finish(self):
        """Summary, post-run output."""
        pass