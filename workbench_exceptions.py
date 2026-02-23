class WorkbenchValidationException(Exception):
    """Custom exception for validation errors in Workbench."""

    # wrapped is a property to indicate that this exception is already wrapped in a user-friendly message and the message should be logged as is.
    wrapped = property(lambda self: object(), lambda self, v: None, lambda self: None)
