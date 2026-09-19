# SPDX-License-Identifier: MIT
"""Fake HTTP client for tests."""


class RecordingHttpClient:
    """An HTTP client that records posted payloads instead of sending them."""

    def __init__(self) -> None:
        """Create an empty client."""
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post_json(self, url: str, payload: dict[str, object]) -> None:
        """Record a POST."""
        self.calls.append((url, payload))
