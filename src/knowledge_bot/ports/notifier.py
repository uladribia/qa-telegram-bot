# SPDX-License-Identifier: MIT
"""Minimal channel-neutral text notification port."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """Send one plain notification to an opaque principal."""

    async def send_text(self, principal_id: str, text: str) -> bool:
        """Return whether the provider accepted the notification."""
        ...
