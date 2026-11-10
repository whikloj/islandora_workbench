from pathlib import Path
import sqlite3
from typing import Union

from requests.models import parse_url


class NodeIdMap:

    ctx: dict
    db_path: str
    table_name: str = "csv_id_to_node_id_map"

    def __init__(self, ctx: dict):
        self.ctx = ctx
        if self.ctx["config"]["query_csv_id_to_node_id_map_for_parents"]:
            db_filepath = ctx["config"]["csv_id_to_node_id_map_path"]
            path = Path(db_filepath).expanduser().resolve()
            if path.exists() and path.is_file():
                self.ctx["log"].info(f"Loading NodeIdMap from {db_filepath}")
            elif not path.exists() and path.parent.exists() and path.parent.is_dir():
                self.ctx["log"].info(f"Creating new NodeIdMap at {db_filepath}")
            else:
                raise ValueError(f"Invalid path for NodeIdMap: {db_filepath}")
            path = str(path)  # convert from Path to string
        else:
            self.ctx["log"].info("Using in-memory NodeIdMap, this will not persist between runs and will not be used for parent node lookups.")
            path = ":memory:"
        self.db_path = path

        self.prepare()

    def prepare(self):
        """Prepare the database and table for use."""
        conn = self.get_connection()  # Create the database file if it doesn't exist
        create_table_sql = (
                "CREATE TABLE IF NOT EXISTS csv_id_to_node_id_map (timestamp TIMESTAMP DEFAULT (datetime('now','localtime')) NOT NULL, " + " config_file TEXT, parent_csv_id TEXT, parent_node_id TEXT, csv_id TEXT, node_id TEXT, host TEXT)")
        conn.execute(create_table_sql)
        pragma_result = conn.execute("select * from pragma_table_info(?)", (self.table_name,))
        pragma_columns = [row[1] for row in pragma_result.fetchall()]
        required_columns = ["timestamp", "config_file", "parent_csv_id", "parent_node_id", "csv_id", "node_id", "host"]
        missing_columns = [col for col in required_columns if col not in pragma_columns]
        if missing_columns:
            if len(missing_columns) == 1 and missing_columns[0] == "host":
                self.ctx["log"].info(f"Adding missing column 'host' to {self.table_name} table in NodeIdMap database.")
                conn.execute(f"ALTER TABLE {self.table_name} ADD COLUMN host TEXT")
        conn.close()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def get_allowed_hosts(self):
        """Derive a query snippet for SQL SELECT queries from values in the "csv_id_to_node_id_map_allowed_hosts" config
            setting. This snippet is inserted into queries against the CSV ID to node ID map to limit results to rows that
            have the one of the hosts named in "csv_id_to_node_id_map_allowed_hosts" in their "host" column. This snippet is
            intended to be located in the middle of the query, specifically, it ends in "and".
            Parameters
                ----------
                config : dict
                    The configuration settings defined by workbench_config.get_config().
                Returns
                -------
                The SQL query string, e.g. " host is null or host in ("https://localhost","https://localhost.dev") and "
                Note that this is a literal string, and does not contain any parameterized SQL query placeholders.
                If "csv_id_to_node_id_map_allowed_hosts" is an empty list, this snippet is empty, which in effect
                does not limit the SQL query against the CSV ID to node ID map to specific "host" field values.
            """

        if len(self.ctx["config"]["csv_id_to_node_id_map_allowed_hosts"]) > 0:
            # Since the user needs to add the current host to this list, we need to make sure it doesn't
            # contain any trailing / or path information. Assumes the protocol (e.g. https) is the same.
            allowed_hosts = set()  # Set means we don't have to worry about duplicates.
            for host in self.ctx["config"]["csv_id_to_node_id_map_allowed_hosts"]:
                parsed = parse_url(host)
                if not parsed.scheme and not parsed.host and not parsed.port:
                    allowed_hosts.add("")
                elif not parsed.scheme or not parsed.host:
                    raise ValueError(f"Invalid host value in csv_id_to_node_id_map_allowed_hosts: {host}")
                else:
                    normalized_host = f"{parsed.scheme}://{parsed.host}" + (f":{parsed.port}" if parsed.port else "")
                    allowed_hosts.add(normalized_host)
            # "" represents an empty host value.
            empty_host_query_string = ""
            if "" in allowed_hosts:
                allowed_hosts.remove("")
                empty_host_query_string = "host is null or "
            hosts_in_parameter = ",".join(allowed_hosts)
            sql_snippet = f" ({empty_host_query_string}host in ({hosts_in_parameter})) and "
        else:
            sql_snippet = ""

        return sql_snippet

    def get_node_id(self, csv_id: str) -> Union[str, list[str], None]:
        """Get the node ID for a given CSV ID from the database.

        :param csv_id: The CSV ID of the node.
        :return str|list|None: The Drupal node ID of the node, a list of node IDs, or None if not found.
        """
        conn = self.get_connection()
        return_one = self.ctx["config"]["ignore_duplicate_parent_ids"]
        if return_one:
            # this is the default behaviour.
            query = (
                    f"select node_id from {self.table_name} where {self.get_allowed_hosts()} csv_id = ? order by timestamp desc limit 1")
        else:
            query = (
                    f"select node_id from {self.table_name} where {self.get_allowed_hosts()} csv_id = ?")
        parent_in_id_map_result = conn.execute(query, (csv_id,))
        if not return_one:
            result = parent_in_id_map_result.fetchall()
            return [row[0] for row in result] if result else None

        result = parent_in_id_map_result.fetchone()
        return result[0] if result else None

    def get_node_id_recovery_mode(self, csv_id: str, parent_csv_id: Union[str, None] = None) -> Union[str, None]:
        """Get the node ID for a given CSV ID from the database that is larger than the configured recovery mode node_id.

        :param csv_id: The CSV ID of the node.
        :param parent_csv_id: The CSV ID of the parent node.
        :return str: The Drupal node ID of the node, or None if not found.
        """
        conn = self.get_connection()
        if parent_csv_id is None:
            query = (
                f"select node_id from {self.table_name} where {self.get_allowed_hosts()} csv_id = ? and node_id + 0 > ? order by timestamp desc limit 1")
            values = (csv_id, int(self.ctx["config"]["recovery_mode_starting_from_node_id"]))
        else:
            query = (
                f"select node_id from {self.table_name} where {self.get_allowed_hosts()} parent_csv_id = ? AND csv_id = ? and node_id + 0 > ? order by timestamp desc limit 1")
            values = (parent_csv_id, csv_id, int(self.ctx["config"]["recovery_mode_starting_from_node_id"]))
        parent_in_id_map_result = conn.execute(query, values)
        result = parent_in_id_map_result.fetchone()
        return result[0] if result else None

    def add_mapping(self, parent_id, parent_csv_id, node_id, node_csv_id) -> None:
        """Add a mapping to the database.
        :param parent_id: The Drupal node ID of the parent node.
        :param parent_csv_id: The CSV ID of the parent node.
        :param node_id: The Drupal node ID of the child node.
        :param node_csv_id: The CSV ID of the child node.
        """
        conn = self.get_connection()
        conn.execute(
            f"INSERT INTO {self.table_name} (config_file, parent_node_id, parent_csv_id, node_id, csv_id, host) VALUES (?, ?, ?, ?, ?)",
            (self.ctx["config"]["config_file"], parent_id, parent_csv_id, node_id, node_csv_id, self.ctx["config"]["host"]))
        conn.commit()
        conn.close()

    def get_all_mappings(self) -> list:
        """Get all mappings from the database for the current config file, ordered by timestamp descending.
        :return list: list of all mappings
        """
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row  # Row factory to access columns by name
        query = f"select * from {self.table_name} where config_file = ? order by timestamp desc"
        result = conn.execute(query, (self.ctx["config"]["config_file"],))
        return result.fetchall()

    def get_all_hosts(self) -> list:
        """Get all unique hosts from the database for the current config file.
        :return list: list of unique hosts
        """
        conn = self.get_connection()
        query = f"select distinct host from {self.table_name} where config_file = ?"
        result = conn.execute(query, (self.ctx["config"]["config_file"],))
        return [row[0] for row in result.fetchall()]

    def get_count(self) -> int:
        """Get the count of all mappings from the database for the current config file.
        :return int: count of mappings
        """
        conn = self.get_connection()
        query = f"select count(*) from {self.table_name} where config_file = ?"
        result = conn.execute(query, (self.ctx["config"]["config_file"],))
        return int(result.fetchone()[0])

    def get_last_ids(self):
        """Get the last node_id and csv_id from the database for the current config file.
        :return tuple: (node_id, csv_id) or (None, None) if no records exist.
        """
        conn = self.get_connection()
        query = f"select node_id, csv_id from {self.table_name} where config_file = ? order by node_id desc limit 1"
        last_ids_result = conn.execute(query, (self.ctx["config"]["config_file"],))
        result = last_ids_result.fetchone()
        return result if result else (None, None)