# SPDX-License-Identifier: MIT
"""Domain errors raised by adapters and translated at transport boundaries."""


class ProjectionError(RuntimeError):
    """A derived search projection failed after semantic state was durable."""

    def __init__(self, code: str) -> None:
        """Store a short safe error code."""
        super().__init__(code)
        self.code = code


class ModelUnavailableError(RuntimeError):
    """An AI model call failed (quota, outage, or timeout)."""

    def __init__(self, operation: str = "model call") -> None:
        """Create the error."""
        super().__init__(f"AI {operation} unavailable")
        self.operation = operation


class InvalidTransitionError(RuntimeError):
    """A requested state transition is not valid for the current record."""


class AuthorizationError(RuntimeError):
    """The acting principal is not allowed to perform an operation."""


class NotFoundError(RuntimeError):
    """A requested durable object does not exist."""
