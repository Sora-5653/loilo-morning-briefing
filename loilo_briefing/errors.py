class LoiLoError(Exception):
    """Base error for expected collector failures."""


class AuthenticationRequired(LoiLoError):
    """The stored LoiLoNote session is missing or expired."""


class NetworkError(LoiLoError):
    """A request could not be completed after bounded retries."""


class HttpError(LoiLoError):
    def __init__(self, status: int, message: str = "HTTP request failed") -> None:
        super().__init__(f"{message}: {status}")
        self.status = status


class SchemaError(LoiLoError):
    """The remote response no longer matches the validated shape."""


class UnsafeRequest(LoiLoError):
    """A request violated the read-only allowlist."""


class CollectionFailed(LoiLoError):
    """No fresh LoiLoNote data could be safely collected."""

