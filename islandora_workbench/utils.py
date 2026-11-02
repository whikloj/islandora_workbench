import copy
import logging
from typing import Union

from islandora_workbench.data_io.csv_data import CsvReader


class Reporter:
    """A simple reporter that provides a message to logs and/or console."""

    def __init__(self, logger=None, console=None):
        self.logger = logger or logging.getLogger("islandora_workbench")
        self.console = console or print

    def debug(self, message, log_message=None, log_only=False):
        """Log a debug message.
        :param message: Message to be logged
        :param log_message: Optional message to be logged (if different from message)
        :param log_only: If True, only log the message without printing to console
        """
        self._process_message(message, log_message=log_message, log_only=log_only, level="debug")

    def info(self, message, log_message=None, log_only=False):
        """Log an info message.
        :param message: Message to be logged
        :param log_message: Optional message to be logged (if different from message)
        :param log_only: If True, only log the message without printing to console
        """
        self._process_message(message, log_message=log_message, log_only=log_only, level="info")

    def warning(self, message, log_message=None, log_only=False):
        """Log a warning message.
        :param message: Message to be logged
        :param log_message: Optional message to be logged (if different from message)
        :param log_only: If True, only log the message without printing to console
        """
        self._process_message(message, log_message=log_message, log_only=log_only, level="warning")

    def error(self, message, log_message=None, log_only=False):
        """Log an error message.
        :param message: Message to be logged
        :param log_message: Optional message to be logged (if different from message)
        :param log_only: If True, only log the message without printing to console
        """
        self._process_message(message, log_message=log_message, log_only=log_only, level="error")


    def _process_message(self, message, log_message=None, print_only=False, log_only=False, level="info"):
        """Process the message for logging and/or console output.
        :param message: Message to be logged
        :param log_message: Optional message to be logged (if different from message)
        :param print_only: If True, only print the message to console without logging
        :param log_only: If True, only log the message without printing to console
        :param level: Logging level (info, error, debug)
        """
        if log_message is None:
            log_message = message
        if not print_only or log_only:
            if level == "info":
                self.logger.info(log_message)
            elif level == "warning":
                self.logger.warning(log_message)
            elif level == "error":
                self.logger.error(log_message)
            elif level == "debug":
                self.logger.debug(log_message)
        if print_only or not log_only:
            # Prepend the level to the message for console output (i.e. info => Info, error => Error, etc.)
            self.console(level[0].upper() + level[1:] + ": " + message)

class Context:
    """A simple context for passing a Reporter, configuration and other data around."""

    _config: dict
    _log: Reporter
    _csv: Union[None, CsvReader]

    def __init__(self, config: dict, log: Union[Reporter, None] = None, csv: Union[None, CsvReader] = None):
        self._config = copy.deepcopy(config)
        self._log = log or Reporter()
        self._csv = csv

    @property
    def config(self):
        return self._config

    @property
    def log(self):
        return self._log

    @property
    def csv(self):
        return self._csv
