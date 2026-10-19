class WorkbenchValidationException(Exception):
    """Custom exception for validation errors in Workbench."""

    _wrapped = False

    def __init__(self, message: str = "", wrapped: bool = False):
        super().__init__(message)
        # wrapped indicates that this exception is already a compilation of one or more validation errors (in a user-friendly message), and the message should be logged as is.
        self._wrapped = wrapped

    @property
    def wrapped(self) -> bool:
        return self._wrapped
