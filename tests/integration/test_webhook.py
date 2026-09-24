# SPDX-License-Identifier: MIT
"""Integration tests for the Telegram webhook route (in-memory fakes)."""

import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient

from knowledge_bot.adapters.inbound.fastapi_routes import create_app
from knowledge_bot.application.classifier import MessageClassifier
from knowledge_bot.domain.entities import Message
from knowledge_bot.domain.enums import IntentLabel
from knowledge_bot.infrastructure.composition import AppContext
from tests.fakes.ai import FakeEmbedder
from tests.fakes.context import SPACE_A, WEBHOOK_SECRET, build_test_context
from tests.fakes.support import InMemoryAiUsageRepository

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
    reply_to: int | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": message_id,
        "date": 1789000000,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": from_id, "is_bot": False},
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {
            "message_id": reply_to,
            "date": 1789000000,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": from_id + 1, "is_bot": False},
        }
    return {"update_id": 1, "message": message}


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


def test_allowlisted_but_unbound_group_is_not_served() -> None:
    """A channel allowlist entry does not replace logical-space binding."""
    context, _ = build_test_context()
    context = replace(
        context,
        identity=replace(
            context.identity,
            allowed_chat_ids=frozenset({"-100", "-300"}),
        ),
    )
    response = _client(context).post(
        "/telegram/webhook",
        json=_update("/ask hola", chat_id=-300),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    assert _stored(context, "-300:10") is None


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
    conversation = asyncio.run(context.ingestor.conversations.get("-100"))
    assert conversation is not None and conversation.space_id == SPACE_A


def test_empty_text_does_not_crash_reviewer_command_parsing() -> None:
    """An empty Telegram text is ignored safely."""
    context, _ = build_test_context()
    response = _client(context).post(
        "/telegram/webhook", json=_update(""), headers=SECRET_HEADER
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert _stored(context) is None


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


def _listener_context(tag: str, **vectors: list[float]) -> AppContext:
    """Build a listener context with a tiny-prototype classifier."""
    context, _ = build_test_context(background_listener_enabled=True)
    embedder = FakeEmbedder(by_text=dict(vectors))
    return replace(
        context,
        classifier=MessageClassifier(
            embedder=embedder,
            prototypes={
                IntentLabel.QUESTION: (f"qp-{tag}",),
                IntentLabel.KNOWLEDGE_UPDATE: (f"up-{tag}",),
                IntentLabel.CORRECTION: (f"cp-{tag}",),
                IntentLabel.CHITCHAT: (f"cc-{tag}",),
            },
        ),
    )


_Q = [1.0, 0.0]
_U = [0.0, 1.0]


def test_listener_stores_pure_chitchat_without_evidence_status() -> None:
    """Classification controls indexing, never whether the raw message is stored."""
    context = _listener_context(
        "discard",
        **{
            "gràcies, cracks!": _Q,
            "qp-discard": _U,
            "up-discard": _U,
            "cp-discard": _U,
            "cc-discard": _Q,
        },
    )
    response = _client(context).post(
        "/telegram/webhook",
        json=_update("gràcies, cracks!"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ingest"}
    stored = _stored(context)
    assert stored is not None
    assert stored.intent_label == "chitchat"
    assert stored.index_status.value == "not_eligible"


def test_listener_labels_kept_context() -> None:
    """A chatty message with a real signal is kept with its intent label."""
    context = _listener_context(
        "labels",
        **{
            "gràcies, demà a les sis?": [0.7, 0.7],
            "qp-labels": _Q,
            "up-labels": _U,
            "cp-labels": _U,
            "cc-labels": _Q,
        },
    )
    response = _client(context).post(
        "/telegram/webhook",
        json=_update("gràcies, demà a les sis?"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ingest"}
    stored = _stored(context)
    assert stored is not None
    assert stored.intent_label == "question"
    assert stored.index_status.value == "not_eligible"


def test_listener_persists_budget_deferred_message_without_ai() -> None:
    """The budget guard defers background work but never drops the message."""
    context = _listener_context("deferred")
    usage = context.budget.usage
    assert isinstance(usage, InMemoryAiUsageRepository)
    usage.seed("2026-09-19", 6_000.0)
    response = _client(context).post(
        "/telegram/webhook",
        json=_update("recordem que demà hi ha entrenament", message_id=29),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ingest"}
    stored = _stored(context, "-100:29")
    assert stored is not None
    assert stored.classification_status.value == "deferred_budget"
    assert stored.index_status.value == "not_indexed"
    matches = asyncio.run(
        context.answer.retrieval.vectors.query(
            [1.0, 0.0], top_k=5, filters={"kind": "message_evidence"}
        )
    )
    assert matches == []


def test_listener_indexes_relevant_background_evidence_immediately() -> None:
    """A standalone knowledge update becomes searchable without a full rebuild."""
    context = _listener_context(
        "index",
        **{
            "recordem que demà hi ha entrenament": _Q,
            "qp-index": _U,
            "up-index": _Q,
            "cp-index": _U,
            "cc-index": _U,
        },
    )
    response = _client(context).post(
        "/telegram/webhook",
        json=_update("recordem que demà hi ha entrenament", message_id=30),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ingest"}
    stored = _stored(context, "-100:30")
    assert stored is not None and stored.index_status.value == "indexed"
    matches = asyncio.run(
        context.answer.retrieval.vectors.query(
            [1.0, 0.0],
            top_k=5,
            filters={"kind": "message_evidence", "scope_key": "space:" + SPACE_A},
        )
    )
    assert len(matches) == 1
    assert matches[0].metadata["authority"] == 40


def test_admin_background_evidence_uses_connector_sender_authority() -> None:
    """The Telegram connector declares admin authority without app-side branching."""
    context = _listener_context(
        "admin",
        **{
            "recordem que l'horari ha canviat": _Q,
            "qp-admin": _U,
            "up-admin": _Q,
            "cp-admin": _U,
            "cc-admin": _U,
        },
    )
    response = _client(context).post(
        "/telegram/webhook",
        json=_update("recordem que l'horari ha canviat", message_id=31, from_id=1),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ingest"}
    matches = asyncio.run(
        context.answer.retrieval.vectors.query(
            [1.0, 0.0],
            top_k=5,
            filters={"kind": "message_evidence"},
        )
    )
    assert matches[0].metadata["authority"] == 95


def test_listener_matches_a_reply_to_its_parent_question() -> None:
    """An answer-like reply to a stored question is paired with it."""
    context = _listener_context(
        "pair",
        **{
            "a quina hora entrenen?": _Q,
            "finalment a les sis": [0.9, 0.4],
            "qp-pair": _Q,
            "up-pair": _Q,
            "cp-pair": _U,
            "cc-pair": _U,
        },
    )
    client = _client(context)
    parent = client.post(
        "/telegram/webhook",
        json=_update("a quina hora entrenen?", message_id=20),
        headers=SECRET_HEADER,
    )
    assert parent.json() == {"status": "ingest"}
    reply = _update("finalment a les sis", message_id=21, reply_to=20)
    response = client.post("/telegram/webhook", json=reply, headers=SECRET_HEADER)
    assert response.json() == {"status": "ingest_pair"}
    stored = asyncio.run(context.ingestor.messages.get("-100:21"))
    assert stored is not None
    assert stored.context_question == "a quina hora entrenen?"
    assert stored.index_status.value == "indexed"


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


def test_a_strangers_dm_is_ignored_and_never_stored() -> None:
    """A public bot username must not open a free-for-all DM channel.

    Anyone who finds the bot could otherwise spend the shared free AI quota and
    send the admin fake correction reviews.
    """
    context, transport = build_test_context()
    response = _client(context).post(
        "/telegram/webhook",
        json=_update(
            "quan entrenen?",
            message_id=77,
            chat_id=999,
            chat_type="private",
            from_id=999,
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    assert _stored(context, "999:77") is None
    assert transport.messages == []


def test_an_allowed_user_may_dm_the_bot() -> None:
    """Users on the allowlist can start a private conversation."""
    context, _ = build_test_context(allowed_user_ids=frozenset({"777"}))
    response = _client(context).post(
        "/telegram/webhook",
        json=_update(
            "quan entrenen?",
            message_id=78,
            chat_id=777,
            chat_type="private",
            from_id=777,
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "answer"}
    assert _stored(context, "777:78") is not None
