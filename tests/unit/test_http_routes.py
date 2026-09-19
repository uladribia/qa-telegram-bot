# SPDX-License-Identifier: MIT
"""Tests for the HTTP routes (health and webhook)."""

from fastapi.testclient import TestClient

from knowledge_bot.adapters.inbound.fastapi_routes import create_app
from tests.fakes.context import build_test_context


def test_healthz_returns_ok() -> None:
    """The health endpoint reports the Worker is alive."""
    context, _ = build_test_context()
    response = TestClient(create_app(lambda request: context)).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_eval_routes_are_refused_once_the_ai_budget_is_spent() -> None:
    """A stray eval run cannot burn the quota the bot needs for real answers."""
    context, _ = build_test_context(spent_neurons=9_000.0)
    client = TestClient(create_app(lambda request: context))
    headers = {"X-Internal-Key": "internal"}
    for path in ("/internal/eval/answer", "/internal/reindex"):
        response = client.post(path, headers=headers, json={"question": "on entrenen?"})
        assert response.status_code == 429, path
        assert "budget" in response.json()["detail"]


def test_eval_routes_run_while_budget_remains() -> None:
    """Under the ceiling the eval endpoints still work."""
    context, _ = build_test_context()
    client = TestClient(create_app(lambda request: context))
    response = client.post(
        "/internal/eval/answer",
        headers={"X-Internal-Key": "internal"},
        json={"question": "on entrenen?"},
    )
    assert response.status_code == 200
