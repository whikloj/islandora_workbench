from typing import Union

from islandora_workbench.state.node_id_map import NodeIdMap
from islandora_workbench.utils import Reporter

def initialize_context(config: dict, log: Union[Reporter, None] = None, node_map: Union[NodeIdMap, None] = None) -> dict:
    """Initialize the context object with the given configuration, an optional logger, and optional node ID map.
    :param config: The configuration settings defined by workbench_config.get_config().
    :param log: An optional Reporter instance for logging. If not provided configure a new Reporter.
    :param node_map: An optional NodeIdMap instance for managing node IDs. If not provided, create a new NodeIdMap.
    :return: The initialized context object.

    Other potential context keys added for specific tasks:
    - "output_csv_writer": An instance of OutputCSVWriter for writing to the output CSV.
    - "progress_bar": An instance of an InitBar for displaying progress.
    - "rollback": An instance of RollbackMgr for writing rollback CSV and configurations.
    """
    ctx = {
        "config": config,
        "log": log if log is not None else Reporter(config),
        "node_map": node_map if node_map is not None else NodeIdMap(config),
    }
    return ctx



