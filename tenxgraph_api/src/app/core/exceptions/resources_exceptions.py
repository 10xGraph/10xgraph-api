from .general_exception import GeneralException


class ResourceNotFoundError(GeneralException):
    """
    Exception raised when a requested resource is not found.

    Attributes:
        message (str): Explanation of the error.
        status_code (int): HTTP status code for the error.
        error_code (str): Specific error code for the error.
    """

    # Implementation here
    def __init__(self, message="Resources not found"):
        self.message = message
        self.status_code = 404
        self.error_code = "RESOURCE_NOT_FOUND"
        super().__init__(
            message=self.message,
            status_code=self.status_code,
            error_code=self.error_code,
        )


class ResourceDuplicationError(GeneralException):
    """
    Exception raised for duplicate resource requests.

    This exception is used to indicate that a request has been made for a resource
    that already exists, and such duplication is not allowed.

    Attributes:
        message (str): Explanation of the error. Defaults to "Duplicate request".
        status_code (int): HTTP status code for the error. Defaults to 403.
        error_code (str): Specific error code for duplicate requests.
        Defaults to "DUPLICATE_REQUEST".
    """

    def __init__(self, message="Duplicate request"):
        self.message = message
        self.status_code = 403
        self.error_code = "DUPLICATE_REQUEST"
        super().__init__(
            message=self.message,
            status_code=self.status_code,
            error_code=self.error_code,
        )


class InvalidOperationError(GeneralException):
    """
    Exception raised for invalid operations.

    This exception is used to indicate that a particular operation is not supported.

    Attributes:
        message (str): Explanation of the error. Defaults to "Operation Not Supported".
        status_code (int): HTTP status code for the error. Defaults to 403.
        error_code (str): Specific error code for the exception.
        Defaults to "InvalidOperationError".
    """

    def __init__(self, message="Operation Not Supported"):
        self.message = message
        self.status_code = 403
        self.error_code = "InvalidOperationError"
        super().__init__(
            message=self.message,
            status_code=self.status_code,
            error_code=self.error_code,
        )


class MessageAlreadyProcessedError(GeneralException):
    """A retried message whose turn the thread already finished (HTTP 409).

    The thread stores a user message with the same client ``message_id``, and either its run
    completed or a later user message followed it, so the run would only repeat it. A client
    that wants the finished run's result again sends the same ``Idempotency-Key`` header
    instead.
    """

    def __init__(
        self,
        message=(
            "This message was already processed: the thread already has it and its turn is over. "
            "Send a new message with a new message_id, or retry with the same "
            "Idempotency-Key header to get the finished run's result."
        ),
    ):
        self.message = message
        self.status_code = 409
        self.error_code = "MESSAGE_ALREADY_PROCESSED"
        super().__init__(
            message=self.message,
            status_code=self.status_code,
            error_code=self.error_code,
        )
