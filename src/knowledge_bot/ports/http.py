# SPDX-License-Identifier: MIT
"""Outbound HTTP port."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class HttpClient(Protocol):
    """Sends JSON requests to external services."""

    async def post_json(self, url: str, payload: dict[str, object]) -> None:
        """POST a JSON payload."""
        ...
