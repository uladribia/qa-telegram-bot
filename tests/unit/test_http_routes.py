# SPDX-License-Identifier: MIT
"""Tests for the Worker HTTP routes."""

from fastapi.testclient import TestClient

from knowledge_bot.adapters.inbound.fastapi_routes import app


def test_healthz_returns_ok() -> None:
    """The health endpoint reports the Worker is alive."""
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_smoke_deps_reports_versions() -> None:
    """Core dependencies import in the current runtime."""
    response = TestClient(app).get("/smoke/deps")
    assert response.status_code == 200
    assert {"fastapi", "pydantic", "loguru"} <= response.json().keys()
