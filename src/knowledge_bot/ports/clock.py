# SPDX-License-Identifier: MIT
"""Clock port: the only source of 'now' for the application."""

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Provides the current time."""

    def now(self) -> datetime:
        """Return the current time."""
        ...
