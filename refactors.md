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
`islandora_workbench/utils.py` - This file contains general utility functions, these should be the lowest level functions that are used.
`islandora_workbench/checks/` - This directory contains the code that performs **general** checks on the input data and configuration. Additional task specific checks are in the relevant `tasks/` files.
`islandora_workbench/checks/common.py` - This file contains the common checks that are used by all tasks.
`islandora_workbench/checks/csv_checks.py` - This file contains the general checks that are specific to CSV input files.
`islandora_workbench/checks/validation.py` - This file contains all of the validation functions used to validate the input data and configuration.
`islandora_workbench/data_io` - This directory contains the code that handles reading and writing data to and from files.
`islandora_workbench/data_io/csv/` - This directory contains the code that handles reading and writing CSV files.
`islandora_workbench/data_io/csv/csv_cache.py` - This file contains various functions for the preprocessed CSV data, including name normalization and fingerprinting
`islandora_workbench/data_io/csv/csv_process.py` - This file contains the code to generate the preprocessed CSV data file.
`islandora_workbench/data_io/csv/csv_reader.py` - This file contains the CsvReader class, which is a thin wrapper with the field names, row count and a csv.DictReader.
`islandora_workbench/drupal/` - This directory contains the code that interacts with the Drupal API.
`islandora_workbench/drupal/__init__.py` - This file contains the FILE_FIELDS constant used to label expected file fields in an Islandora instance.
`islandora_workbench/drupal/client.py` - This file contains generic client code for interacting with the Drupal API.
`islandora_workbench/drupal/media.py` - This file contains code for performing media operations with the Drupal API.
`islandora_workbench/drupal/node.py` - This file contains code for performing node operations with the Drupal API.
`islandora_workbench/drupal/taxonomy.py` - This file contains code for performing taxonomy operations with the Drupal API.
`islandora_workbench/drupal/utils.py` - This file contains utility functions related to the Drupal API.
`islandora_workbench/internal/__init__.py` - This file contains utilities for internal use of Workbench, such as `create_temp_dir()` and `check_integration_module_version()`.
`islandora_workbench/state/node_id_map.py` - This file contains the `NodeIdMap` class which is used to keep track of the mapping between CSV IDs and the Drupal Node IDs.
`islandora_workbench/tasks/` - This directory contains the code that performs the actual tasks of the Workbench, such as creating nodes, uploading files, and setting relationships.
