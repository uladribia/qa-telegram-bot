# SPDX-License-Identifier: MIT
"""Tests for the HTTP routes (health and webhook)."""

from fastapi.testclient import TestClient

from knowledge_bot.adapters.inbound.fastapi_routes import create_app
from tests.fakes.context import build_test_context


def test_healthz_returns_ok() -> None:
    """The health endpoint reports the Worker is alive."""
    context, _ = build_test_context()
    response = TestClient(create_app(context)).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
