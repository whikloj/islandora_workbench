import argparse

from islandora_workbench.startup import startup


def build_parser() -> argparse.ArgumentParser:
    """Builds the command-line argument parser for the Islandora Workbench tool."""
    parser = argparse.ArgumentParser(
        prog="workbench",
        description="Islandora Workbench: A tool for batch processing and managing digital assets in Islandora."
    )
    # ---- Config ----------
    parser.add_argument("--config", required=True, help="Configuration file to use.")
    # ---- Behaviour -------
    parser.add_argument(
        "--check", help="Check input data and exit without creating/updating/etc.", action="store_true", )
    parser.add_argument(
        "--get_csv_template", help="Generate a CSV template using the specified configuration file.",
        action="store_true", )
    parser.add_argument(
        "--quick_delete_node", help="Delete the node (and all attached media) identified by the URL).", )
    parser.add_argument(
        "--quick_delete_media", help="Delete the media (and attached file) identified by the URL).", )
    parser.add_argument(
        "--contactsheet", help="Generate a contact sheet.", action="store_true"
    )
    parser.add_argument(
        "--print_config", help="Prints out the current configuration settings.", action="store_true", )
    parser.add_argument(
        "--skip_user_prompts",
        help='Include to skip any user prompts defined in your config file\'s "user_prompts" setting.',
        action="store_true", )
    # ---- Overrides ----------
    parser.add_argument(
        "--input_csv", help="Overrides the 'input_csv' configuration setting.", )
    parser.add_argument(
        "--input_dir", help="Overrides the 'input_dir' configuration setting.", )
    parser.add_argument(
        "--google_sheets_gid", help="Overrides the 'google_sheets_gid' configuration setting.", )
    parser.add_argument(
        "--log_file_path", help="Overrides the 'log_file_path' configuration setting.", )
    parser.add_argument(
        "--rollback_config_file_path", help="Overrides the 'rollback_config_file_path' configuration setting.", )
    parser.add_argument(
        "--rollback_csv_file_path", help="Overrides the 'rollback_csv_file_path' configuration setting.", )
    parser.add_argument(
        "--csv_start_row", help="Overrides the 'csv_start_row' configuration setting.", )
    parser.add_argument(
        "--csv_stop_row", help="Overrides the 'csv_stop_row' configuration setting.", )
    parser.add_argument(
        "--recovery_mode_starting_from_node_id",
        help="Overrides the 'recovery_mode_starting_from_node_id' configuration setting.", )

    parser.add_argument("--version", action="version", version="Islandora Workbench 0.0.0")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        startup(args)
    except Exception as e:
        print(f"Error occurred while initializing WorkbenchConfig: {e}")
        return

