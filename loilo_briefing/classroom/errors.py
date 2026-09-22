class ClassroomError(Exception):
    """A sanitized collection failure; never include an API response or URL."""


class AuthenticationRequired(ClassroomError):
    pass


class SetupRequired(ClassroomError):
    pass


class RequestFailed(ClassroomError):
    pass


class SchemaError(ClassroomError):
    pass
