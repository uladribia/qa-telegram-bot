# SPDX-License-Identifier: MIT
"""HTTP client backed by the Workers ``fetch`` API.

``workers.fetch`` is imported lazily so this module stays importable in CPython
for tests.
"""

import json
from http import HTTPMethod


class WorkersHttpClient:
    """Send JSON requests with the Workers runtime fetch."""

    async def post_json(
        self, url: str, payload: dict[str, object]
    ) -> dict[str, object]:
        """POST a JSON payload and return the decoded JSON response."""
        from workers import fetch

        response = await fetch(
            url,
            method=HTTPMethod.POST,
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload),
        )
        try:
            decoded = await response.json()
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}
