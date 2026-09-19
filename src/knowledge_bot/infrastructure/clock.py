# SPDX-License-Identifier: MIT
"""Clock backed by the system time."""

from datetime import UTC, datetime


class SystemClock:
    """Return the current UTC time."""

    def now(self) -> datetime:
        """Return the current time."""
        return datetime.now(UTC)
