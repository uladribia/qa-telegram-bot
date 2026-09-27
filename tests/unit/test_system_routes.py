# SPDX-License-Identifier: MIT
"""Tests for the platform probe routes."""

from fastapi.testclient import TestClient

from knowledge_bot.api.app import create_app
from tests.fakes.context import build_test_context


def test_healthz_returns_ok() -> None:
    """The health endpoint reports the Worker is alive."""
    context, _ = build_test_context()
    response = TestClient(create_app(lambda request: context)).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_ok() -> None:
    """The readiness endpoint answers without consuming AI quota."""
    context, _ = build_test_context()
    response = TestClient(create_app(lambda request: context)).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
