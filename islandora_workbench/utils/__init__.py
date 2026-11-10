import logging
from collections import deque
from typing import Union

from rich import Console


class Reporter:
    """A simple reporter that provides a message to logs and/or console."""

    def __init__(self, logger=None, console=None):
        self.logger = logger or logging.getLogger("islandora_workbench")
        self.console = console or Console()

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
            self.console.print(level[0].upper() + level[1:] + ": " + message)


class ResponseTimeTracker:
    """A class to track and calculate the average response time of the most recent HTTP requests."""

    def __init__(self, logger: logging.Logger, log_sample: bool = False):
        self.logger = logger
        self.log_sample = log_sample
        self.response_times: deque[float] = deque(maxlen=20)

    def calculate_response_time_trend(self, response_time: Union[int, float]) -> float:
        """Gets the average response time from the most recent 20 HTTP requests.
        :param response_time: Response time of current request, in seconds.
        :return float: The average response time of the most recent requests up to 20.
        """
        self.response_times.append(response_time)
        if self.log_sample:
            self.logger.info("Response time trend sample: %s", self.response_times)
        average = sum(self.response_times) / len(self.response_times)
        return average
