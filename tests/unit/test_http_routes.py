# SPDX-License-Identifier: MIT
"""Tests for the HTTP routes (health and webhook)."""

import pytest
from fastapi.testclient import TestClient

from knowledge_bot.adapters.http import app as app_module
from knowledge_bot.adapters.http.app import create_app
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


def test_retrieve_refuses_more_queries_than_one_chunk() -> None:
    """One request may not carry more queries than a single safe chunk."""
    context, _ = build_test_context()
    client = TestClient(create_app(lambda request: context))
    queries = [f"què val {index}?" for index in range(app_module._MAX_EVAL_QUERIES + 1)]
    response = client.post(
        "/internal/retrieve",
        headers={"X-Internal-Key": "internal"},
        json={"queries": queries},
    )
    assert response.status_code == 422
    assert "at most" in response.json()["detail"]


def test_eval_calls_are_throttled_per_isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of evaluation calls is refused once the allowance is spent."""
    monkeypatch.setattr(app_module, "_EVAL_CALLS_PER_MINUTE", 2)
    context, _ = build_test_context()
    client = TestClient(create_app(lambda request: context))
    headers = {"X-Internal-Key": "internal"}
    for _ in range(2):
        assert (
            client.post(
                "/internal/eval/answer", headers=headers, json={"question": "què val?"}
            ).status_code
            == 200
        )
    refused = client.post(
        "/internal/eval/answer", headers=headers, json={"question": "què val?"}
    )
    assert refused.status_code == 429
    assert "too many evaluation calls" in refused.json()["detail"]


def test_eval_throttle_does_not_block_unauthenticated_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requests without the internal key never consume the allowance."""
    monkeypatch.setattr(app_module, "_EVAL_CALLS_PER_MINUTE", 1)
    context, _ = build_test_context()
    client = TestClient(create_app(lambda request: context))
    for _ in range(3):
        assert (
            client.post(
                "/internal/eval/answer", json={"question": "què val?"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/internal/eval/answer",
            headers={"X-Internal-Key": "internal"},
            json={"question": "què val?"},
        ).status_code
        == 200
    )


def test_groups_endpoint_registers_a_group() -> None:
    """The internal groups endpoint registers a served group."""
    context, _ = build_test_context()
    client = TestClient(create_app(lambda request: context))
    headers = {"X-Internal-Key": "internal"}
    assert client.post("/internal/groups").status_code == 401
    assert (
        client.post(
            "/internal/groups", headers=headers, json={"chat_id": ""}
        ).status_code
        == 422
    )
    response = client.post(
        "/internal/groups",
        headers=headers,
        json={"chat_id": "-100", "title": "Prebenjamins"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "registered"
    assert response.json()["space_id"].startswith("sp_")
    # Idempotent re-registration succeeds.
    response = client.post(
        "/internal/groups", headers=headers, json={"chat_id": "-100"}
    )
    assert response.status_code == 200
