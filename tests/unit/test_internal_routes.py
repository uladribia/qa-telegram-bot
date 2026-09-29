# SPDX-License-Identifier: MIT
"""Tests for the internal operator routes."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from knowledge_bot.api.app import create_app
from knowledge_bot.api.routes import internal as internal_module
from knowledge_bot.application.review import ReviewService
from knowledge_bot.domain.enums import BotMode
from knowledge_bot.domain.scope import scope_for_space
from knowledge_bot.ports.review import ReviewItem
from tests.fakes.ai import FakeReviewSource
from tests.fakes.context import SPACE_A, build_test_context

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)


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
    queries = [
        f"què val {index}?" for index in range(internal_module._MAX_EVAL_QUERIES + 1)
    ]
    response = client.post(
        "/internal/retrieve",
        headers={"X-Internal-Key": "internal"},
        json={"queries": queries},
    )
    assert response.status_code == 422
    assert "at most" in response.json()["detail"]


def test_eval_calls_are_throttled_per_isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of evaluation calls is refused once the allowance is spent."""
    monkeypatch.setattr(internal_module, "_EVAL_CALLS_PER_MINUTE", 2)
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
    monkeypatch.setattr(internal_module, "_EVAL_CALLS_PER_MINUTE", 1)
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
    binding = asyncio.run(context.spaces.resolve("telegram", "-100"))
    assert binding is not None and binding.title == "Prebenjamins"


def test_groups_endpoint_changes_the_mode_without_renaming() -> None:
    """The mode is set through the same idempotent registration."""
    context, _ = build_test_context()
    client = TestClient(create_app(lambda request: context))
    headers = {"X-Internal-Key": "internal"}
    client.post(
        "/internal/groups",
        headers=headers,
        json={"chat_id": "-100", "title": "Prebenjamins"},
    )
    response = client.post(
        "/internal/groups",
        headers=headers,
        json={"chat_id": "-100", "bot_mode": "proactive"},
    )
    assert response.status_code == 200
    binding = asyncio.run(context.spaces.resolve("telegram", "-100"))
    assert binding is not None
    assert binding.bot_mode is BotMode.PROACTIVE
    assert binding.title == "Prebenjamins"


def test_groups_endpoint_rejects_an_unknown_mode() -> None:
    """A mode the bot does not implement is a configuration error."""
    client = TestClient(create_app(lambda request: build_test_context()[0]))
    response = client.post(
        "/internal/groups",
        headers={"X-Internal-Key": "internal"},
        json={"chat_id": "-100", "bot_mode": "shouty"},
    )
    assert response.status_code == 422


def _review_item(key: str, scope: str, answer: str, *, question: str) -> ReviewItem:
    """Build one current Q&A record for the review report."""
    return ReviewItem(
        canonical_key=key,
        question=question,
        scope=scope,
        answer=answer,
        origin="web_seed",
        created_at=NOW,
        status="active",
    )


def test_internal_review_route_renders_the_report_with_group_titles() -> None:
    """``kb review`` reaches the report, and a variant is labelled by its group.

    The label is the group's registered title, not its scope key: a report
    naming ``space:sp_…`` is unreadable for the admin who has to act on it.
    """
    context, _ = build_test_context()
    scope = scope_for_space(SPACE_A)
    context = replace(
        context,
        review=ReviewService(
            FakeReviewSource(
                [
                    _review_item("k1", "global", "Resposta global", question="Què?"),
                    _review_item("k1", scope, "Resposta del grup", question="Què?"),
                ]
            ),
            context.spaces.spaces,
        ),
    )
    client = TestClient(create_app(lambda request: context))
    assert client.post("/internal/review").status_code == 401
    response = client.post("/internal/review", headers={"X-Internal-Key": "internal"})
    assert response.status_code == 200
    report = response.json()["report"]
    assert "**global**: Resposta global" in report
    assert "**grup Group -100**: Resposta del grup" in report
    assert "variant de grup diferent de la global" in report
