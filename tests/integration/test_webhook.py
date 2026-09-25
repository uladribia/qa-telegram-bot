# SPDX-License-Identifier: MIT
"""Integration tests for the Telegram webhook route (in-memory fakes)."""

import asyncio
from collections.abc import Awaitable
from dataclasses import replace

from fastapi.testclient import TestClient

from knowledge_bot.adapters.http.app import create_app
from knowledge_bot.application.classifier import MessageClassifier
from knowledge_bot.domain.entities import Message
from knowledge_bot.infrastructure.context import AppContext
from tests.fakes.ai import FakeEmbedder, linear_head
from tests.fakes.context import SPACE_A, WEBHOOK_SECRET, build_test_context
from tests.fakes.support import InMemoryAiUsageRepository

SECRET_HEADER = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}


async def _awaited(processing: Awaitable[str]) -> str:
    """Await one deferred update handler and return its result."""
    return await processing


def _client(context: AppContext) -> TestClient:
    return TestClient(create_app(lambda request: context))


def test_update_is_acknowledged_before_it_is_processed() -> None:
    """A deferred update is acknowledged at once and still processed fully.

    Telegram retries a webhook whose response arrives late, so the Worker must
    answer before the AI pipeline runs without dropping the update.
    """
    context, _ = build_test_context()
    deferred: list[Awaitable[str]] = []

    def defer(processing: Awaitable[str]) -> None:
        deferred.append(processing)

    client = TestClient(create_app(lambda request: context, defer))
    response = client.post(
        "/telegram/webhook", json=_update("/ask quan entrenen?"), headers=SECRET_HEADER
    )

    assert response.json() == {"status": "accepted"}
    assert _stored(context) is None, "the pipeline ran before the response"
    assert len(deferred) == 1
    assert asyncio.run(_awaited(deferred[0])) == "answer"
    stored = _stored(context)
    assert stored is not None
    assert stored.text == "/ask quan entrenen?"


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


def test_delivery_failure_replays_the_persisted_answer_once() -> None:
    """A failed Telegram send can be retried without regenerating the answer."""
    context, transport = build_test_context()
    transport.answer_failures_remaining = 1
    client = _client(context)
    update = _update("/ask quan entrenen?")

    first = client.post("/telegram/webhook", json=update, headers=SECRET_HEADER)
    assert first.status_code == 503
    assert asyncio.run(context.answer.answers.get("ans:-100:10")) is not None
    assert transport.answers == []

    second = client.post("/telegram/webhook", json=update, headers=SECRET_HEADER)
    assert second.status_code == 200
    assert len(transport.answers) == 1
    receipt = asyncio.run(
        context.delivery_receipts.get("answer", "ans:-100:10", "telegram")
    )
    assert receipt is not None

    third = client.post("/telegram/webhook", json=update, headers=SECRET_HEADER)
    assert third.status_code == 200
    assert len(transport.answers) == 1
    assert (
        asyncio.run(context.delivery_receipts.get("answer", "ans:-100:10", "telegram"))
        == receipt
    )


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


def _listener_context(**vectors: list[float]) -> AppContext:
    """Build a listener context with a controllable linear classifier head."""
    context, _ = build_test_context(background_listener_enabled=True)
    embedder = FakeEmbedder(vector=[0.25, 0.25, 0.25, 0.25], by_text=dict(vectors))
    return replace(
        context,
        classifier=MessageClassifier(
            embedder=embedder,
            head=linear_head(len(embedder.vector)),
        ),
    )


_Q = [1.0, 0.0, 0.0, 0.0]
_U = [0.0, 1.0, 0.0, 0.0]
_C = [0.0, 0.0, 1.0, 0.0]
_CC = [0.0, 0.0, 0.0, 1.0]


def test_listener_stores_pure_chitchat_without_evidence_status() -> None:
    """Classification controls indexing, never whether the raw message is stored."""
    context = _listener_context(
        **{
            "gràcies, cracks!": _CC,
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
        **{
            "gràcies, demà a les sis?": [0.7, 0.7, 0.0, 0.0],
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
    context = _listener_context()
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


def test_bounded_backlog_processes_deferred_background_messages() -> None:
    """An explicit bounded request handles deferred background messages."""
    context = build_test_context(
        background_listener_enabled=True,
        spent_neurons=6_000.0,
    )[0]
    client = _client(context)
    response = client.post(
        "/telegram/webhook",
        json=_update("recordem que demà hi ha entrenament", message_id=28),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ingest"}
    usage = context.budget.usage
    assert isinstance(usage, InMemoryAiUsageRepository)
    usage.seed("2026-09-19", 0.0)

    processed = client.post(
        "/internal/background/process-backlog",
        json={"limit": 10},
        headers={"X-Internal-Key": "internal"},
    )

    assert processed.json() == {"processed": 1, "stopped_by_budget": False}
    stored = _stored(context, "-100:28")
    assert stored is not None
    assert stored.classification_status.value == "classified"


def test_listener_indexes_relevant_background_evidence_immediately() -> None:
    """A standalone knowledge update becomes searchable without a full rebuild."""
    context = _listener_context(
        **{
            "recordem que demà hi ha entrenament": _U,
            "qp-index": _U,
            "up-index": _U,
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
            [0.0, 1.0, 0.0, 0.0],
            top_k=5,
            filters={"kind": "message_evidence", "scope_key": "space:" + SPACE_A},
        )
    )
    assert len(matches) == 1
    assert matches[0].metadata["authority"] == 40


def test_admin_background_evidence_uses_connector_sender_authority() -> None:
    """The Telegram connector declares admin authority without app-side branching."""
    context = _listener_context(
        **{
            "recordem que l'horari ha canviat": _U,
            "qp-admin": _U,
            "up-admin": _U,
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
            [0.0, 1.0, 0.0, 0.0],
            top_k=5,
            filters={"kind": "message_evidence"},
        )
    )
    assert matches[0].metadata["authority"] == 95


def test_listener_matches_a_reply_to_its_parent_question() -> None:
    """An answer-like reply to a stored question is paired with it."""
    context = _listener_context(
        **{
            "a quina hora entrenen?": _Q,
            "finalment a les sis": _U,
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


def test_legacy_recap_route_is_retired() -> None:
    """Legacy recap scheduling is no longer an active endpoint."""
    context, _ = build_test_context()
    assert _client(context).post("/internal/recap").status_code == 404


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
