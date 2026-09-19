# SPDX-License-Identifier: MIT
"""Domain errors raised by adapters and translated at transport boundaries."""


class ModelUnavailableError(RuntimeError):
    """An AI model call failed (quota, outage, or timeout).

    Callers must degrade safely: keep the inbound event, never invent an
    answer, and tell the user the answer is temporarily unavailable.
    """

    def __init__(self, operation: str = "model call") -> None:
        """Create the error.

        Args:
            operation: The failed operation, for boundary logging.
        """
        super().__init__(f"AI {operation} unavailable")
        self.operation = operation
