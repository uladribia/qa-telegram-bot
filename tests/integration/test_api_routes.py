# SPDX-License-Identifier: MIT
"""Integration tests for generic channel-independent HTTP routes."""

import asyncio

from fastapi.testclient import TestClient

from knowledge_bot.adapters.http.app import create_app
from knowledge_bot.domain.entities import BotAnswer
from knowledge_bot.domain.enums import AnswerMode
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.context import build_test_context

KEY = {"X-Internal-Key": "internal"}


def _context_with_answer() -> AppContext:
    context, _ = build_test_context()
    asyncio.run(
        context.answer.retrieval.vectors.upsert(
            [
                VectorRecord(
                    id="qa:item-1",
                    values=[1.0, 0.0],
                    metadata={
                        "kind": "qa",
                        "object_id": "item-1",
                        "version_id": "v1",
                        "canonical_key": "equipment",
                        "status": "active",
                        "scope_key": "global",
                        "authority": 90,
                        "question": "Quan?",
                        "text": "Dimarts",
                    },
                )
            ]
        )
    )
    return context


def test_question_request_is_idempotent() -> None:
    """A repeated request returns the stored answer without another AI decision."""
    context = _context_with_answer()
    client = TestClient(create_app(lambda request: context))
    body = {"request_id": "req-1", "question": "Quan?"}

    first = client.post("/v1/questions", json=body, headers=KEY)
    second = client.post("/v1/questions", json=body, headers=KEY)

    assert first.status_code == 200
    assert first.json()["mode"] == "synthesis"
    assert second.json()["answer_id"] == first.json()["answer_id"]


def test_request_id_reuse_with_different_question_conflicts() -> None:
    """An idempotency key cannot silently change meaning."""
    context = _context_with_answer()
    client = TestClient(create_app(lambda request: context))
    client.post(
        "/v1/questions",
        json={"request_id": "req-1", "question": "Quan?"},
        headers=KEY,
    )

    response = client.post(
        "/v1/questions",
        json={"request_id": "req-1", "question": "On?"},
        headers=KEY,
    )

    assert response.status_code == 409


def test_feedback_start_and_proposal_are_channel_independent() -> None:
    """The generic feedback flow uses principals rather than chat ids."""
    context, _ = build_test_context()
    asyncio.run(
        context.answer.answers.add(
            BotAnswer(
                id="ans-1",
                conversation_id="api:global",
                question="Quan?",
                answer="Dimarts",
                answer_mode=AnswerMode.DIRECT_QA,
                created_at=context.clock.now(),
            )
        )
    )
    client = TestClient(create_app(lambda request: context))
    started = client.post(
        "/v1/feedback",
        json={
            "answer_id": "ans-1",
            "reporter_principal_id": "web:user-1",
        },
        headers=KEY,
    )
    assert started.status_code == 200

    proposed = client.put(
        f"/v1/feedback/{started.json()['feedback_id']}/proposal",
        json={"reporter_principal_id": "web:user-1", "proposal": "Dijous"},
        headers=KEY,
    )

    assert proposed.status_code == 200
    assert proposed.json()["status"] == "pending_review"
