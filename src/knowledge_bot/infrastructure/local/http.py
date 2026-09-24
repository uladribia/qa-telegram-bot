# SPDX-License-Identifier: MIT
"""Async JSON HTTP client for optional local channel integrations."""

import httpx

from knowledge_bot.ports.http import HttpClient


class HttpxClient(HttpClient):
    """POST JSON through a shared asynchronous HTTP client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Wrap an HTTPX client."""
        self._client = client

    async def post_json(
        self, url: str, payload: dict[str, object]
    ) -> dict[str, object]:
        """POST JSON and decode the response."""
        response = await self._client.post(url, json=payload)
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, dict) else {"result": value}
