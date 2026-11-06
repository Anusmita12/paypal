from typing import Any


class AppError(Exception):
    """A business-rule failure with a stable error code for the API, UI, and agent tools."""

    def __init__(self, code: str, message: str, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def not_found(what: str) -> AppError:
    # Also used for records owned by another session, so existence is not leaked.
    return AppError("not_found", f"{what} not found", 404)
