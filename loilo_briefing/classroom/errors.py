class ClassroomError(Exception):
    """A sanitized collection failure; never include an API response or URL.

    ``reason`` is an optional fixed machine code (never free text) that tells
    the user what to fix without exposing tokens, responses, or file paths.
    """

    def __init__(self, message="", *, reason=None):
        super().__init__(message)
        self.reason = reason


class AuthenticationRequired(ClassroomError):
    pass


class SetupRequired(ClassroomError):
    pass


class RequestFailed(ClassroomError):
    pass


class SchemaError(ClassroomError):
    pass
