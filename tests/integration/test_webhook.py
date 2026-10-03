# SPDX-License-Identifier: MIT
"""Integration tests for the Telegram webhook route (in-memory fakes)."""

import asyncio
from collections.abc import Awaitable
from dataclasses import replace

from fastapi.testclient import TestClient

from knowledge_bot.adapters.telegram.flow import DM_ACCESS_NOTICE
from knowledge_bot.adapters.telegram.routes import TELEGRAM_WEBHOOK_PATH
from knowledge_bot.api.app import create_app
from knowledge_bot.application.assessment import BaselineAssessmentModel
from knowledge_bot.application.classifier import MessageClassifier
from knowledge_bot.application.listener import ListenerIngestor
from knowledge_bot.domain.entities import Message, TelegramInteraction
from knowledge_bot.domain.enums import BotMode
from knowledge_bot.domain.scope import GLOBAL_SCOPE
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import FakeEmbedder, linear_head
from tests.fakes.context import (
    DEFAULT_NOW,
    SPACE_A,
    SPACE_B,
    WEBHOOK_SECRET,
    build_test_context,
)
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
        TELEGRAM_WEBHOOK_PATH,
        json=_update("/ask quan entrenen?"),
        headers=SECRET_HEADER,
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
        TELEGRAM_WEBHOOK_PATH,
        json=_update("/ask hola"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert response.status_code == 401
    assert _stored(context) is None


def test_disallowed_chat_is_ignored() -> None:
    """Updates from another chat are ignored."""
    context, _ = build_test_context()
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("/ask hola", chat_id=-1),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    assert _stored(context, "-1:10") is None


def test_addressed_message_is_answered_and_persisted() -> None:
    """An addressed message is stored and marked to be answered."""
    context, _ = build_test_context()
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("/ask quan entrenen?"),
        headers=SECRET_HEADER,
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

    first = client.post(TELEGRAM_WEBHOOK_PATH, json=update, headers=SECRET_HEADER)
    assert first.status_code == 503
    assert asyncio.run(context.answer.answers.get("ans:-100:10")) is not None
    assert transport.answers == []

    second = client.post(TELEGRAM_WEBHOOK_PATH, json=update, headers=SECRET_HEADER)
    assert second.status_code == 200
    assert len(transport.answers) == 1
    receipt = asyncio.run(
        context.delivery_receipts.get("answer", "ans:-100:10", "telegram")
    )
    assert receipt is not None

    third = client.post(TELEGRAM_WEBHOOK_PATH, json=update, headers=SECRET_HEADER)
    assert third.status_code == 200
    assert len(transport.answers) == 1
    assert (
        asyncio.run(context.delivery_receipts.get("answer", "ans:-100:10", "telegram"))
        == receipt
    )


def test_empty_text_does_not_crash_reviewer_command_parsing() -> None:
    """An empty Telegram text is stored without text, never a crash."""
    context, _ = build_test_context()
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH, json=_update(""), headers=SECRET_HEADER
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ingest"}
    stored = _stored(context)
    assert stored is not None
    assert stored.text == ""
    assert stored.classification_status.value == "no_text"


def test_empty_text_in_an_off_group_is_ignored() -> None:
    """A group turned off drops the message, empty or not."""
    context, _ = build_test_context(group_bot_mode=BotMode.OFF)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH, json=_update(""), headers=SECRET_HEADER
    )
    assert response.json() == {"status": "ignored"}
    assert _stored(context) is None


def test_bare_question_is_ignored_by_default() -> None:
    """A group turned off neither stores nor answers unaddressed traffic."""
    context, _ = build_test_context(group_bot_mode=BotMode.OFF)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("quan entrenen?"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    assert _stored(context) is None


def test_listener_ingests_unaddressed_messages() -> None:
    """An active group stores unaddressed traffic without answering it."""
    context, _ = build_test_context(group_bot_mode=BotMode.ACTIVE)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("quan entrenen?"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ingest"}
    assert _stored(context) is not None


def _listener_context(**vectors: list[float]) -> AppContext:
    """Build a listener context with a controllable linear classifier head."""
    context, _ = build_test_context(group_bot_mode=BotMode.ACTIVE)
    embedder = FakeEmbedder(vector=[0.25, 0.25, 0.25, 0.25], by_text=dict(vectors))
    classifier = MessageClassifier(
        embedder=embedder,
        head=linear_head(len(embedder.vector)),
    )
    assessment = BaselineAssessmentModel(classifier)
    return replace(
        context,
        classifier=classifier,
        assessment=assessment,
        listener=ListenerIngestor(
            ingestor=context.ingestor,
            assessment=assessment,
            budget=context.budget,
            pairing=context.pairing,
            background_indexer=context.background_indexer,
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
        TELEGRAM_WEBHOOK_PATH,
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
        TELEGRAM_WEBHOOK_PATH,
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
        TELEGRAM_WEBHOOK_PATH,
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
        group_bot_mode=BotMode.ACTIVE,
        spent_neurons=6_000.0,
    )[0]
    client = _client(context)
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
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
        TELEGRAM_WEBHOOK_PATH,
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
        TELEGRAM_WEBHOOK_PATH,
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
        TELEGRAM_WEBHOOK_PATH,
        json=_update("a quina hora entrenen?", message_id=20),
        headers=SECRET_HEADER,
    )
    assert parent.json() == {"status": "ingest"}
    reply = _update("finalment a les sis", message_id=21, reply_to=20)
    response = client.post(TELEGRAM_WEBHOOK_PATH, json=reply, headers=SECRET_HEADER)
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
            TELEGRAM_WEBHOOK_PATH, json=_update("/ask hola"), headers=SECRET_HEADER
        )
        assert response.status_code == 200
    assert _stored(context) is not None


def test_legacy_recap_route_is_retired() -> None:
    """Legacy recap scheduling is no longer an active endpoint."""
    context, _ = build_test_context()
    assert _client(context).post("/internal/recap").status_code == 404


def test_a_strangers_dm_gets_one_notice_and_nothing_else() -> None:
    """A public bot username must not open a free-for-all DM channel.

    Anyone who finds the bot could otherwise spend the shared free AI quota and
    send the admin fake correction reviews. The notice explains how to be
    recognised, is stored nowhere, and costs no model call.
    """
    context, transport = build_test_context()
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update(
            "quan entrenen?",
            message_id=77,
            chat_id=999,
            chat_type="private",
            from_id=999,
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "dm_notice_sent"}
    assert _stored(context, "999:77") is None
    assert transport.answers == []
    assert transport.messages == [("999", DM_ACCESS_NOTICE)]


def test_the_stranger_notice_is_not_repeated_every_time() -> None:
    """A stranger who keeps writing gets one notice a day, not one per message."""
    context, transport = build_test_context()
    client = _client(context)
    for index in (77, 78, 79):
        client.post(
            TELEGRAM_WEBHOOK_PATH,
            json=_update(
                "hola?",
                message_id=index,
                chat_id=999,
                chat_type="private",
                from_id=999,
            ),
            headers=SECRET_HEADER,
        )
    assert transport.messages == [("999", DM_ACCESS_NOTICE)]


def test_a_member_who_wrote_in_a_served_group_may_dm_the_bot() -> None:
    """Membership observed in a group is what opens the private channel."""
    context, transport = build_test_context()
    client = _client(context)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("hola", from_id=555),
        headers=SECRET_HEADER,
    )
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update(
            "quan entrenen?",
            message_id=77,
            chat_id=555,
            chat_type="private",
            from_id=555,
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "dm_answer"}
    assert _stored(context, "555:77") is not None
    assert all(chat == "555" for chat, _, _ in transport.answers)


def test_an_allowed_user_may_dm_the_bot() -> None:
    """Users on the allowlist can start a private conversation."""
    context, _ = build_test_context(allowed_user_ids=frozenset({"777"}))
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update(
            "quan entrenen?",
            message_id=78,
            chat_id=777,
            chat_type="private",
            from_id=777,
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "dm_answer"}
    assert _stored(context, "777:78") is not None


def _memberships(context: AppContext, principal: str) -> list[str]:
    """Return the spaces a principal is an active member of."""
    return asyncio.run(context.memberships.memberships.list_active_spaces(principal))


def test_an_off_group_still_learns_who_writes_in_it() -> None:
    """Membership is observed before the mode is applied, not after."""
    context, transport = build_test_context(group_bot_mode=BotMode.OFF)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("hola"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    assert _stored(context) is None
    assert transport.messages == []
    assert _memberships(context, "telegram:111") == [SPACE_A]


def test_a_joined_member_is_recorded() -> None:
    """A join service message is how someone is seen without writing."""
    context, _ = build_test_context(group_bot_mode=BotMode.OFF)
    message = _update("Ignored", message_id=12)
    message["message"] = {
        "message_id": 12,
        "date": 1789000000,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": 555, "is_bot": False},
        "new_chat_members": [{"id": 555, "is_bot": False}],
    }
    _client(context).post(TELEGRAM_WEBHOOK_PATH, json=message, headers=SECRET_HEADER)
    assert _memberships(context, "telegram:555") == [SPACE_A]


def test_a_departed_member_stops_being_a_member() -> None:
    """A leave service message withdraws what the group authorized."""
    context, _ = build_test_context(group_bot_mode=BotMode.OFF)
    client = _client(context)
    client.post(TELEGRAM_WEBHOOK_PATH, json=_update("hola"), headers=SECRET_HEADER)
    assert _memberships(context, "telegram:111") == [SPACE_A]
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json={
            "update_id": 2,
            "message": {
                "message_id": 13,
                "date": 1789000000,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 999, "is_bot": False},
                "left_chat_member": {"id": 111, "is_bot": False},
            },
        },
        headers=SECRET_HEADER,
    )
    assert _memberships(context, "telegram:111") == []


def test_a_muted_group_ingests_a_message_that_addresses_the_bot() -> None:
    """``silent`` stores everything and answers nothing, mentioned or not."""
    context, transport = build_test_context(group_bot_mode=BotMode.SILENT)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("@bot hola"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ingest"}
    assert transport.answers == []
    assert _stored(context) is not None


def test_an_active_group_does_not_answer_an_unaddressed_question() -> None:
    """``active`` is the previous behaviour: listen, answer only when asked."""
    context, transport = build_test_context(group_bot_mode=BotMode.ACTIVE)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("quan entrenen?"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ingest"}
    assert transport.answers == []


def test_an_active_group_answers_a_mention() -> None:
    """A mention is an answer in every group that is not off or muted."""
    context, transport = build_test_context(group_bot_mode=BotMode.ACTIVE)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("@bot /ask hola"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "answer"}
    assert len(transport.answers) == 1


def _seed_answerable_qa(context: AppContext) -> None:
    """Put one strong global Q&A vector in the context's retrieval index."""
    asyncio.run(
        context.answer.retrieval.vectors.upsert(
            [
                VectorRecord(
                    id="qa:web-item",
                    values=[1.0, 0.0],
                    metadata={
                        "kind": "qa",
                        "object_id": "web-item",
                        "version_id": "qav:web-1",
                        "canonical_key": "horari",
                        "status": "active",
                        "scope_key": "global",
                        "text": "Els dimarts a les sis.",
                        "authority": 90,
                        "question": "Quan entrenen?",
                    },
                )
            ]
        )
    )


def test_a_proactive_group_answers_a_confident_question() -> None:
    """``proactive`` is the one mode that speaks without being asked."""
    context, transport = build_test_context(group_bot_mode=BotMode.PROACTIVE)
    _seed_answerable_qa(context)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("Quan entrenen?"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "proactive_answered"}
    assert len(transport.answers) == 1
    assert transport.answers[0][2] == "ans:-100:10:p:-100"
    assert _stored(context) is not None


def test_a_proactive_group_stays_silent_when_it_cannot_answer() -> None:
    """Uninvited silence beats an uninvited "I don't know"."""
    context, transport = build_test_context(group_bot_mode=BotMode.PROACTIVE)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("Quan entrenen?"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "proactive_suppressed"}
    assert transport.messages == []


def test_a_proactive_group_stays_silent_on_chitchat() -> None:
    """Chitchat is classified, stored, and never answered uninvited."""
    context = _listener_context(
        **{"hola": [0.0, 0.0, 0.0, 1.0], "hi": [0.0, 0.0, 0.0, 1.0]}
    )
    client = _client(context)
    client.post(TELEGRAM_WEBHOOK_PATH, json=_update("hola"), headers=SECRET_HEADER)
    stored = _stored(context)
    assert stored is not None and stored.intent_label == "chitchat"


def test_a_proactive_group_speaks_only_once_per_message() -> None:
    """A retried update does not generate or send a second uninvited answer."""
    context, transport = build_test_context(group_bot_mode=BotMode.PROACTIVE)
    _seed_answerable_qa(context)
    client = _client(context)
    for _ in range(2):
        client.post(
            TELEGRAM_WEBHOOK_PATH, json=_update("Quan entrenen?"), headers=SECRET_HEADER
        )
    assert len(transport.answers) == 1


def test_proactive_stops_spending_before_the_background_class() -> None:
    """Uninvited answers have their own budget class, and it is the first cut."""
    context, transport = build_test_context(
        group_bot_mode=BotMode.PROACTIVE, spent_neurons=4_000.0
    )
    _seed_answerable_qa(context)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("Quan entrenen?"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "proactive_budget_blocked"}
    assert transport.messages == []


def test_an_invited_question_ignores_the_proactive_budget() -> None:
    """A question the user asked is never refused by the daily guard."""
    context, transport = build_test_context(
        group_bot_mode=BotMode.PROACTIVE, spent_neurons=9_999.0
    )
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("@bot /ask hola"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "answer"}
    assert len(transport.answers) == 1


def test_an_off_group_still_serves_its_control_plane() -> None:
    """Turning a group off does not disable what the bot already promised."""
    context, _ = build_test_context(group_bot_mode=BotMode.OFF)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_update("/reviewer", from_id=1),
        headers=SECRET_HEADER,
    )
    assert response.status_code == 200
    assert _memberships(context, "telegram:1") == [SPACE_A]


def _seed_scoped_qa(
    context: AppContext, *, space_id: str | None = None, key: str = "horari"
) -> None:
    """Put one strong Q&A vector in a scope of the context's retrieval index.

    Two records sharing a ``key`` are the same question, and a local one then
    overrides the global answer, which is how a group correction reaches a
    private answer. Two different keys are two different questions.
    """
    asyncio.run(
        context.answer.retrieval.vectors.upsert(
            [
                VectorRecord(
                    id="qa:global" if space_id is None else f"qa:space:{key}",
                    values=[1.0, 0.0],
                    metadata={
                        "kind": "qa",
                        "object_id": key,
                        "version_id": "qav:1",
                        "canonical_key": key,
                        "status": "active",
                        "scope_key": GLOBAL_SCOPE
                        if space_id is None
                        else f"space:{space_id}",
                        "text": "Els dimarts a les sis.",
                        "authority": 90,
                        "question": f"Quan entrenen? {key}",
                    },
                )
            ]
        )
    )


def _dm(
    text: str,
    *,
    message_id: int = 90,
    from_id: int = 111,
    reply_to: int | None = None,
) -> dict[str, object]:
    return _update(
        text,
        message_id=message_id,
        chat_id=from_id,
        chat_type="private",
        from_id=from_id,
        reply_to=reply_to,
    )


def _join_group(
    context: AppContext, *, from_id: int = 111, chat_id: int = -100
) -> None:
    """Record the sender as an active member of the group that chat serves."""
    asyncio.run(
        context.memberships.observe(
            f"telegram:{from_id}", SPACE_A if chat_id == -100 else SPACE_B
        )
    )


def test_a_private_question_is_answered_in_every_group_the_asker_belongs_to() -> None:
    """One round per served group, so a group correction is not missed."""
    context, transport = build_test_context()
    _join_group(context, from_id=111, chat_id=-100)
    _join_group(context, from_id=111, chat_id=-200)
    _seed_scoped_qa(context, key="horari")
    _seed_scoped_qa(context, space_id=SPACE_A, key="horari")
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH, json=_dm("Quan entrenen?"), headers=SECRET_HEADER
    )
    assert response.json() == {"status": "dm_answer"}
    assert [answer_id for _, _, answer_id in transport.answers] == [
        "ans:111:90:-200",
        "ans:111:90:-100",
    ]


def test_identical_answers_are_delivered_once() -> None:
    """The same words from the same sources are one answer, not one per group."""
    context, transport = build_test_context()
    _join_group(context, from_id=111, chat_id=-100)
    _join_group(context, from_id=111, chat_id=-200)
    _seed_scoped_qa(context)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH, json=_dm("Quan entrenen?"), headers=SECRET_HEADER
    )
    assert response.json() == {"status": "dm_answer"}
    assert len(transport.answers) == 1
    assert "\U0001f310" not in transport.answers[0][1]


def test_a_local_variant_is_delivered_separately_and_labelled() -> None:
    """A group that knows something extra gets its own block, labelled."""
    context, transport = build_test_context()
    _join_group(context, from_id=111, chat_id=-100)
    _join_group(context, from_id=111, chat_id=-200)
    _seed_scoped_qa(context, key="horari")
    _seed_scoped_qa(context, space_id=SPACE_A, key="material")
    _client(context).post(
        TELEGRAM_WEBHOOK_PATH, json=_dm("Quan entrenen?"), headers=SECRET_HEADER
    )
    delivered = {answer_id: text for _, text, answer_id in transport.answers}
    assert len(delivered) == 2
    # The group round found both the club's answer and its own, so it says so.
    assert delivered["ans:111:90:-100"].startswith(
        "\U0001f310 Global \u00b7 \U0001f465 Group -100\n"
    )
    # The other group found only the club's answer, and is labelled as such.
    assert delivered["ans:111:90:-200"].startswith("\U0001f310 Global\n")


def test_a_single_answer_carries_no_scope_heading() -> None:
    """One answer is delivered as it is: a heading would only be noise.

    A group that corrected the club's answer wins the round outright, so the
    local record suppresses the global one and the reader gets that answer and
    nothing else around it.
    """
    context, transport = build_test_context()
    _join_group(context, from_id=111, chat_id=-200)
    _seed_scoped_qa(context, key="horari")
    _seed_scoped_qa(context, space_id=SPACE_B, key="horari")
    _client(context).post(
        TELEGRAM_WEBHOOK_PATH, json=_dm("Quan entrenen?"), headers=SECRET_HEADER
    )
    assert len(transport.answers) == 1
    text = transport.answers[0][1]
    assert text.startswith("resposta de prova")
    assert "\U0001f310" not in text
    assert "\U0001f465" not in text
    assert transport.answers[0][2] == "ans:111:90:-200"


def test_a_retried_private_question_is_not_answered_twice() -> None:
    """A redelivered update re-sends nothing that already went out."""
    context, transport = build_test_context()
    _join_group(context, from_id=111, chat_id=-100)
    _seed_scoped_qa(context, key="horari")
    _seed_scoped_qa(context, space_id=SPACE_A, key="material")
    client = _client(context)
    for _ in range(2):
        client.post(
            TELEGRAM_WEBHOOK_PATH, json=_dm("Quan entrenen?"), headers=SECRET_HEADER
        )
    assert len(transport.answers) == 1


def test_a_retry_sends_only_the_answer_that_never_went_out() -> None:
    """One failed bundle does not cost the reader the one that succeeded."""
    context, transport = build_test_context()
    _join_group(context, from_id=111, chat_id=-100)
    _join_group(context, from_id=111, chat_id=-200)
    _seed_scoped_qa(context, key="horari")
    _seed_scoped_qa(context, space_id=SPACE_A, key="material")
    # Let the first bundle through and fail the second, once, as a
    # transient Telegram error would.
    transport.answer_failure_calls = {2}
    client = _client(context)
    payload = _dm("Quan entrenen?")
    # A failed delivery is retryable, so the webhook reports it as one.
    failed = client.post(TELEGRAM_WEBHOOK_PATH, json=payload, headers=SECRET_HEADER)
    assert failed.status_code == 503
    first_pass = list(transport.answers)
    assert len(first_pass) == 1

    client.post(TELEGRAM_WEBHOOK_PATH, json=payload, headers=SECRET_HEADER)

    assert transport.answers[0] == first_pass[0]
    assert len(transport.answers) == 2


def test_a_member_who_left_a_group_loses_that_scope() -> None:
    """A group the asker left stops answering for them."""
    context, transport = build_test_context()
    _join_group(context, from_id=111, chat_id=-100)
    _join_group(context, from_id=111, chat_id=-200)
    _seed_scoped_qa(context, key="horari")
    _seed_scoped_qa(context, space_id=SPACE_A, key="horari")
    asyncio.run(context.memberships.mark_left("telegram:111", SPACE_B))
    _client(context).post(
        TELEGRAM_WEBHOOK_PATH, json=_dm("Quan entrenen?"), headers=SECRET_HEADER
    )
    assert [answer_id for _, _, answer_id in transport.answers] == ["ans:111:90:-100"]


def test_an_asker_with_no_group_is_answered_from_global_knowledge() -> None:
    """An allowlisted person with no group still gets the club's answers."""
    context, transport = build_test_context(allowed_user_ids=frozenset({"777"}))
    _seed_scoped_qa(context)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_dm("Quan entrenen?", from_id=777),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "dm_answer"}
    assert [answer_id for _, _, answer_id in transport.answers] == ["ans:777:90:global"]


def test_a_muted_private_chat_stores_without_answering() -> None:
    """``silent`` keeps the message and never spends a model call on it."""
    context, transport = build_test_context(dm_bot_mode=BotMode.SILENT)
    _join_group(context, from_id=111, chat_id=-100)
    _seed_scoped_qa(context, space_id=SPACE_A)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH, json=_dm("Quan entrenen?"), headers=SECRET_HEADER
    )
    assert response.json() == {"status": "ingest"}
    assert transport.answers == []
    stored = _stored(context, "111:90")
    assert stored is not None
    assert stored.index_status.value == "not_indexed"


def test_a_turned_off_private_chat_is_mute() -> None:
    """``off`` emits nothing at all, not even the notice to a stranger."""
    context, transport = build_test_context(dm_bot_mode=BotMode.OFF)
    client = _client(context)
    for sender in (111, 999):
        response = client.post(
            TELEGRAM_WEBHOOK_PATH,
            json=_dm("Quan entrenen?", from_id=sender),
            headers=SECRET_HEADER,
        )
        assert response.json() == {"status": "ignored"}
    assert transport.messages == []
    assert transport.answers == []


def test_a_proactive_private_chat_answers_like_an_active_one() -> None:
    """A private message always addresses the bot, so the modes converge."""
    context, transport = build_test_context(dm_bot_mode=BotMode.PROACTIVE)
    _join_group(context, from_id=111, chat_id=-100)
    _seed_scoped_qa(context)
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH, json=_dm("Quan entrenen?"), headers=SECRET_HEADER
    )
    assert response.json() == {"status": "dm_answer"}
    assert len(transport.answers) == 1


def test_a_reply_to_a_spent_correction_prompt_is_not_a_question() -> None:
    """Redelivering a correction must not spend a generation on its text."""
    context, transport = build_test_context()
    _join_group(context, from_id=111, chat_id=-100)
    asyncio.run(
        context.interactions.add(
            TelegramInteraction(
                external_message_id="5",
                interaction_type="feedback_proposal",
                object_id="fb:1",
                principal_id="telegram:111",
                created_at=DEFAULT_NOW,
                consumed_at=DEFAULT_NOW,
            )
        )
    )
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_dm("La llista la passa l'entrenador.", message_id=91, reply_to=5),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    assert transport.answers == []


def test_pressing_a_group_button_records_membership() -> None:
    """The person who flags an answer is in the group, whether or not they type."""
    context, _ = build_test_context()
    _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json={
            "update_id": 5,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 555, "is_bot": False},
                "data": "feedback:start:ans:-100:10",
                "message": {
                    "message_id": 42,
                    "date": 1789000000,
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": 999, "is_bot": True},
                    "text": "resposta",
                },
            },
        },
        headers=SECRET_HEADER,
    )
    assert _memberships(context, "telegram:555") == [SPACE_A]
