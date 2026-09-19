# SPDX-License-Identifier: MIT
"""Fake HTTP client for tests."""


class RecordingHttpClient:
    """An HTTP client that records posted payloads instead of sending them."""

    def __init__(self) -> None:
        """Create an empty client."""
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses: list[dict[str, object]] = []

    async def post_json(
        self, url: str, payload: dict[str, object]
    ) -> dict[str, object]:
        """Record a POST."""
        self.calls.append((url, payload))
        return (
            self.responses.pop(0)
            if self.responses
            else {"ok": True, "result": {"message_id": 1}}
        )
