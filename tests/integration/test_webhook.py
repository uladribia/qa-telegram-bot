# SPDX-License-Identifier: MIT
"""Integration tests for the Telegram webhook route (in-memory fakes)."""

import asyncio

from fastapi.testclient import TestClient

from knowledge_bot.adapters.inbound.fastapi_routes import create_app
from knowledge_bot.domain.entities import Message
from knowledge_bot.infrastructure.composition import AppContext
from tests.fakes.context import WEBHOOK_SECRET, build_test_context

SECRET_HEADER = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}


def _client(context: AppContext) -> TestClient:
    return TestClient(create_app(lambda request: context))


def _stored(context: AppContext, message_id: str = "-100:10") -> Message | None:
    return asyncio.run(context.ingestor.messages.get(message_id))


def _update(
    text: str,
    *,
    message_id: int = 10,
    chat_id: int = -100,
    chat_type: str = "supergroup",
    from_id: int = 111,
) -> dict[str, object]:
    return {
        "update_id": 1,
        "message": {
            "message_id": message_id,
            "date": 1789000000,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": from_id, "is_bot": False},
            "text": text,
        },
    }


def test_invalid_secret_is_rejected() -> None:
    """A wrong webhook secret returns 401 and stores nothing."""
    context, _ = build_test_context()
    response = _client(context).post(
        "/telegram/webhook",
        json=_update("/ask hola"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert response.status_code == 401
    assert _stored(context) is None


def test_disallowed_chat_is_ignored() -> None:
    """Updates from another chat are ignored."""
    context, _ = build_test_context()
    response = _client(context).post(
        "/telegram/webhook",
        json=_update("/ask hola", chat_id=-1),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    assert _stored(context, "-1:10") is None


def test_addressed_message_is_answered_and_persisted() -> None:
    """An addressed message is stored and marked to be answered."""
    context, _ = build_test_context()
    response = _client(context).post(
        "/telegram/webhook", json=_update("/ask quan entrenen?"), headers=SECRET_HEADER
    )
    assert response.json() == {"status": "answer"}
    stored = _stored(context)
    assert stored is not None
    assert stored.text == "/ask quan entrenen?"


def test_bare_question_is_ignored_by_default() -> None:
    """Without the background listener, an unaddressed question is ignored."""
    context, _ = build_test_context()
    response = _client(context).post(
        "/telegram/webhook", json=_update("quan entrenen?"), headers=SECRET_HEADER
    )
    assert response.json() == {"status": "ignored"}
    assert _stored(context) is None


def test_listener_ingests_unaddressed_messages() -> None:
    """With the background listener on, unaddressed traffic is stored."""
    context, _ = build_test_context(background_listener_enabled=True)
    response = _client(context).post(
        "/telegram/webhook", json=_update("quan entrenen?"), headers=SECRET_HEADER
    )
    assert response.json() == {"status": "ingest"}
    assert _stored(context) is not None


def test_reprocessing_the_same_update_is_idempotent() -> None:
    """A repeated update does not duplicate the message."""
    context, _ = build_test_context()
    client = _client(context)
    for _ in range(2):
        response = client.post(
            "/telegram/webhook", json=_update("/ask hola"), headers=SECRET_HEADER
        )
        assert response.status_code == 200
    assert _stored(context) is not None


def test_internal_recap_requires_the_key() -> None:
    """The internal recap endpoint rejects a missing or wrong key."""
    context, _ = build_test_context()
    client = _client(context)
    assert client.post("/internal/recap").status_code == 401
    assert (
        client.post("/internal/recap", headers={"X-Internal-Key": "wrong"}).status_code
        == 401
    )
