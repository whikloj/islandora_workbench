# Islandora Workbench Refactoring

This document covers the effort to refactor the Islandora Workbench codebase to improve
clarity, maintainability and performance. The goal was to split up the many functions contained
in only 4 files into a more modular structure.

## Refactoring Goals

1. The goal was to move all actual code under the `islandora_workbench` namespace but keep the `workbench` file in the root of the module for backwards compatibility.
2. Split the code flow into tasks that can be followed, understood and tested more easily.
3. Pass a shared context object which can hold configuration, logging and other shared tools/state (i.e. node id map, csv reader, rollback writer, output csv writer)

## File Map

`workbench` - This file is a thin wrapper that calls the `islandora_workbench/cli.py::main()` function.
`islandora_workbench/cli.py` - This file contains the main entry point for the CLI and handles argument parsing and dispatching to the appropriate functions.
`islandora_workbench/config.py` - This file contains the configuration handling code, it began with moving the `WorkbenchConfig.py` file from the root of the module.
`islandora_workbench/context.py` - This file sets up the shared context of all tasks. Some tasks may add their own state objects to the context.
`islandora_workbench/exceptions.py` - This file contains the custom exceptions used throughout the module.
`islandora_workbench/external_scripts.py` - This file contains the code that calls external scripts, such as bootstrap or shutdown functions.
`islandora_workbench/utils/utils.py` - This file contains general utility functions, these should be the lowest level functions that are used.
`islandora_workbench/checks/` - This directory contains the code that performs **general** checks on the input data and configuration. Additional task specific checks are in the relevant `tasks/` files.
`islandora_workbench/checks/common.py` - This file contains the common checks that are used by all tasks.
`islandora_workbench/checks/csv_checks.py` - This file contains the general checks that are specific to CSV input files.
`islandora_workbench/checks/validation.py` - This file contains all of the validation functions used to validate the input data and configuration.
`islandora_workbench/client/` - This directory contains the code that interacts with other "APIs".
`islandora_workbench/drupal` - This file contains the code that interacts with the Drupal API.
`islandora_workbench/state/node_id_map.py` - 