# SPDX-License-Identifier: MIT
"""Pytest configuration: assign test tiers by directory.

Tiers:
- ``unit`` and ``architecture`` are fast and run on every change.
- ``integration`` uses in-memory fakes and runs when flows are touched.
- ``smoke`` hits the real Worker runtime and runs at milestone boundaries.
"""

import os

import pytest

# Tests import the runtime entrypoints, which configure telemetry. Keep test
# spans out of the production project; the SDK is still exercised locally.
os.environ.setdefault("KB_LOGFIRE_SEND_TO_LOGFIRE", "false")

_TIER_MARKERS = {
    "unit": pytest.mark.unit,
    "architecture": pytest.mark.architecture,
    "integration": pytest.mark.integration,
    "smoke": pytest.mark.smoke,
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag each collected test with the marker of its tier directory."""
    for item in items:
        path = str(item.path)
        for tier, marker in _TIER_MARKERS.items():
            if f"/tests/{tier}/" in path:
                item.add_marker(marker)
                break
