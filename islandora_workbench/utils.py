import logging

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


    def _process_message(self, message, log_message=None, log_only=False, level="info"):
        """Process the message for logging and/or console output.
        :param message: Message to be logged
        :param log_message: Optional message to be logged (if different from message)
        :param log_only: If True, only log the message without printing to console
        :param level: Logging level (info, error, debug)
        """
        if log_message is None:
            log_message = message
        if level == "info":
            self.logger.info(log_message)
        elif level == "warning":
            self.logger.warning(log_message)
        elif level == "error":
            self.logger.error(log_message)
        elif level == "debug":
            self.logger.debug(log_message)
        if not log_only:
            # Prepend the level to the message for console output (i.e. info => Info, error => Error, etc.)
            self.console(level[0].upper() + level[1:] + ": " + message)
