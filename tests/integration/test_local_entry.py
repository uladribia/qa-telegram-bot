# SPDX-License-Identifier: MIT
"""Integration coverage for the local HTTP entrypoint."""

import pytest
from fastapi.testclient import TestClient

from knowledge_bot.local_entry import app

pytestmark = pytest.mark.integration


def test_local_healthz_does_not_initialize_external_dependencies() -> None:
    """Return local liveness without opening SQLite or Ollama."""
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
