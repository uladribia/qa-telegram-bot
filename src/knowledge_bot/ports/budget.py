# SPDX-License-Identifier: MIT
"""Port for recording estimated Workers AI spend."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class AiUsageRepository(Protocol):
    """Stores the estimated neuron spend for the current UTC day."""

    async def add(self, day: str, neurons: float, calls: int) -> None:
        """Add an estimate to a day's running total."""
        ...

    async def get(self, day: str) -> tuple[float, int]:
        """Return the day's ``(neurons, calls)`` so far."""
        ...
