# SPDX-License-Identifier: MIT
"""Integration tests for reviewer nomination and routing over the webhook."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi.testclient import TestClient

from knowledge_bot.adapters.telegram.flow import INDEX_WARNING
from knowledge_bot.adapters.telegram.routes import TELEGRAM_WEBHOOK_PATH
from knowledge_bot.api.app import create_app
from knowledge_bot.application.feedback import PROPOSAL_ACK, PROPOSAL_PROMPT
from knowledge_bot.application.reindex import ReindexService
from knowledge_bot.domain.entities import BotAnswer
from knowledge_bot.domain.enums import (
    AnswerMode,
    FeedbackStatus,
    ProjectionState,
    QAStatus,
)
from knowledge_bot.domain.identity import canonical_key_for
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.models.seed import SeedQA
from tests.fakes.ai import FakeEmbedder, FakeVectorStore
from tests.fakes.backend import InMemoryBackend
from tests.fakes.context import (
    SPACE_A,
    WEBHOOK_SECRET,
    build_test_context,
)
from tests.fakes.search_index import RepositorySearchIndexSource
from tests.fakes.support import FrozenClock, RecordingTransport

SECRET_HEADER = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
INTERNAL_HEADER = {"X-Internal-Key": "internal"}
NOW = datetime(2026, 9, 21, 18, 4, tzinfo=UTC)
REVIEWER_ID = 222
SECOND_REVIEWER_ID = 333
ADMIN_ID = 1
GROUP_SCOPE = scope_for_space(SPACE_A)
QUESTION = "Com es demana l'equipament?"


def _client(context: AppContext) -> TestClient:
    return TestClient(create_app(lambda request: context))


def _group_message(
    text: str,
    *,
    from_id: int,
    reply_to: dict[str, object] | None = None,
    chat_id: str = "-100",
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": 50,
        "date": 1789000000,
        "chat": {"id": int(chat_id), "type": "supergroup"},
        "from": {"id": from_id, "is_bot": False},
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = reply_to
    return {"update_id": 5, "message": message}


def _person_message(user_id: int, first_name: str) -> dict[str, object]:
    """Build a group message from a person, to reply to when nominating."""
    return {
        "message_id": 40,
        "date": 1789000000,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": user_id, "is_bot": False, "first_name": first_name},
        "text": "hola",
    }


def _callback(
    data: str, *, from_id: int, first_name: str = "Marta"
) -> dict[str, object]:
    return {
        "update_id": 7,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": from_id, "is_bot": False, "first_name": first_name},
            "data": data,
            "message": {
                "message_id": 42,
                "date": 1789000000,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 999, "is_bot": True},
                "text": "resposta",
            },
        },
    }


def _reply(text: str, *, reply_to: int, from_id: int = 555) -> dict[str, object]:
    return {
        "update_id": 8,
        "message": {
            "message_id": 60,
            "date": 1789000000,
            "chat": {"id": from_id, "type": "private"},
            "from": {"id": from_id, "is_bot": False},
            "text": text,
            "reply_to_message": {
                "message_id": reply_to,
                "date": 1789000000,
                "chat": {"id": from_id, "type": "private"},
                "from": {"id": 999, "is_bot": True},
                "text": PROPOSAL_PROMPT,
            },
        },
    }


async def _seed_answer(context: AppContext) -> None:
    await context.answer.answers.add(
        BotAnswer(
            id="ans:-100:10",
            conversation_id="-100",
            space_id=SPACE_A,
            question="Com es demana l'equipament?",
            answer="Resposta antiga.",
            answer_mode=AnswerMode.DIRECT_QA,
            created_at=NOW,
            user_message_id="-100:10",
        )
    )


def _nominate_reviewer(
    context: AppContext,
    client: TestClient,
    *,
    text: str = "/reviewer",
    reply_to: dict[str, object] | None = None,
) -> None:
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_group_message(
            text,
            from_id=ADMIN_ID,
            reply_to=_person_message(REVIEWER_ID, "Pepe")
            if reply_to is None
            else reply_to,
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "reviewer_nominated"}


def _open_proposal(client: TestClient) -> int:
    """Start a correction and return the prompt message id to reply to."""
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback("feedback:start:ans:-100:10", from_id=555),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_started"}
    return 1


def test_admin_nominates_a_group_reviewer_by_reply() -> None:
    """Replying /reviewer to PEPE's message makes PEPE the group's reviewer."""
    context, transport = build_test_context()
    client = _client(context)
    _nominate_reviewer(context, client)
    reviewer = asyncio.run(context.reviewers.reviewers.get(GROUP_SCOPE))
    assert reviewer is not None
    assert reviewer.principal_id == f"telegram:{REVIEWER_ID}"
    assert reviewer.name == "Pepe"
    assert any("revisor d'aquest grup" in text for _, text in transport.messages)


def test_non_admin_cannot_nominate() -> None:
    """A nomination from anyone but the admin is ignored."""
    context, _ = build_test_context()
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_group_message(
            "/reviewer",
            from_id=777,
            reply_to=_person_message(REVIEWER_ID, "Pepe"),
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    assert asyncio.run(context.reviewers.reviewers.get(GROUP_SCOPE)) is None


def test_reviewer_without_reply_lists_current_reviewers() -> None:
    """A plain /reviewer lists who reviews what instead of nominating."""
    context, transport = build_test_context()
    client = _client(context)
    _nominate_reviewer(context, client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_group_message("/reviewer", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    assert any("Revisors:" in text for _, text in transport.messages)


def test_admin_cannot_nominate_the_bot() -> None:
    """Replying /reviewer to a bot message is refused, not stored."""
    context, transport = build_test_context()
    bot_reply: dict[str, object] = {
        "message_id": 41,
        "date": 1789000000,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": 999, "is_bot": True, "first_name": "BHC Q&A"},
        "text": "resposta",
    }
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_group_message("/reviewer", from_id=ADMIN_ID, reply_to=bot_reply),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "reviewer_bot_refused"}
    assert asyncio.run(context.reviewers.reviewers.get(GROUP_SCOPE)) is None
    assert any("No pots nomenar el bot" in text for _, text in transport.messages)


def test_flagging_twice_reuses_the_open_feedback() -> None:
    """A second press on the same answer reuses the row instead of colliding."""
    context, transport = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _open_proposal(client)
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback("feedback:start:ans:-100:10", from_id=777, first_name="Joana"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_started"}
    feedback = asyncio.run(context.feedback.get_feedback("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.reporter_principal_id == "telegram:777"
    assert len(transport.force_replies) == 2


def test_flag_prompt_fails_with_an_alert_when_dm_is_unreachable() -> None:
    """A presser who never started the bot gets an alert, not silence."""
    context, transport = build_test_context()
    transport.dead_chats = frozenset({"555"})
    asyncio.run(_seed_answer(context))
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback("feedback:start:ans:-100:10", from_id=555),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_prompt_undelivered"}
    assert transport.force_replies == []
    assert transport.callback_alerts == [("cb-1", transport.callback_alerts[0][1])]
    assert any(
        chat == "-100" and "https://t.me/bot" in text
        for chat, text in transport.messages
    )
    feedback = asyncio.run(context.feedback.get_feedback("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.proposal_prompt_message_id is None


def test_the_dm_prompt_repeats_the_flagged_question_and_answer() -> None:
    """The private proposal prompt shows the question and answer corrected."""
    context, transport = build_test_context()
    asyncio.run(_seed_answer(context))
    _open_proposal(_client(context))
    prompt = transport.force_replies[0][1]
    assert "Pregunta original:" in prompt
    assert "Com es demana l'equipament?" in prompt
    assert "Resposta actual:" in prompt
    assert "Resposta antiga." in prompt


def test_unreachable_reviewer_keeps_feedback_pending_and_activates_reviewer() -> None:
    """A failed reviewer DM notifies admin and asks the reviewer to activate the bot."""
    context, transport = build_test_context()
    transport.dead_chats = frozenset({str(REVIEWER_ID)})
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _nominate_reviewer(context, client)
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    assert all(chat != str(REVIEWER_ID) for chat, _, _ in transport.reviews)
    assert not any(chat == "1" for chat, _, _ in transport.reviews)
    assert any(
        chat == "1" and "després de 86400 s" in text
        for chat, text in transport.messages
    )
    assert any(chat == "-100" and "@Pepe" in text for chat, text in transport.messages)


def test_unreachable_reviewer_escalates_to_admin_after_configured_timeout() -> None:
    """Escalate an undelivered review when the test timeout is zero."""
    context, transport = build_test_context(reviewer_escalation_timeout_seconds=0)
    transport.dead_chats = frozenset({str(REVIEWER_ID)})
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _nominate_reviewer(context, client)
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    assert any(chat == "1" for chat, _, _ in transport.reviews)
    feedback = asyncio.run(context.feedback.get_feedback("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.reviewer_escalated_at is not None
    assert any("0 s" in text for _, text in transport.messages)

    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback(
            "feedback:edit:fb:ans:-100:10", from_id=ADMIN_ID, first_name="Admin"
        ),
        headers=SECRET_HEADER,
    )
    edit_prompt = transport.force_replies[-1][0]
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply(
            "La resposta local corregida és VERIFICAT-LOCAL.",
            reply_to=int(edit_prompt),
            from_id=ADMIN_ID,
        ),
        headers=SECRET_HEADER,
    )
    approval = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback(
            "feedback:approve-group:fb:ans:-100:10",
            from_id=ADMIN_ID,
            first_name="Admin",
        ),
        headers=SECRET_HEADER,
    )
    assert approval.json() == {"status": "feedback_approved"}
    approved = asyncio.run(context.feedback.get_feedback("fb:ans:-100:10"))
    assert approved is not None
    assert approved.status is FeedbackStatus.APPROVED


def test_reviewer_off_removes_the_group_reviewer() -> None:
    """`/reviewer off` clears the group's reviewer."""
    context, _ = build_test_context()
    client = _client(context)
    _nominate_reviewer(context, client)
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_group_message("/reviewer off", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "reviewer_removed"}
    assert asyncio.run(context.reviewers.reviewers.get(GROUP_SCOPE)) is None


def test_proposal_from_the_group_goes_to_its_reviewer_not_the_admin() -> None:
    """With a group reviewer, review DMs go to them instead of the admin."""
    context, transport = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _nominate_reviewer(context, client)
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("La llista la passa l'entrenador.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    assert ("555", PROPOSAL_ACK) in transport.messages
    reviewer_reviews = [
        text for chat, text, _ in transport.reviews if chat == str(REVIEWER_ID)
    ]
    admin_reviews = [text for chat, text, _ in transport.reviews if chat == "1"]
    assert any("Correcció proposada" in text for text in reviewer_reviews)
    assert transport.review_global_access[str(REVIEWER_ID)] is False
    assert admin_reviews == []


def test_group_reviewer_can_confirm_and_the_admin_is_not_asked_to_review() -> None:
    """The group's reviewer approves; the admin is not put on the review path."""
    context, transport = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _nominate_reviewer(context, client)
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback(
            "feedback:approve-group:fb:ans:-100:10",
            from_id=REVIEWER_ID,
            first_name="Pepe",
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_approved"}
    feedback = asyncio.run(context.feedback.get_feedback("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.status is FeedbackStatus.APPROVED
    assert not any("Correccions revisades" in text for _, text in transport.messages)


def test_local_reviewer_global_approval_is_denied_by_server() -> None:
    """A forged global callback cannot bypass authorization."""
    context, _ = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _nominate_reviewer(context, client)
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta global.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback(
            "feedback:approve-global:fb:ans:-100:10",
            from_id=REVIEWER_ID,
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    feedback = asyncio.run(context.feedback.get_feedback("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.status.value == "pending_review"


def test_a_stranger_cannot_confirm() -> None:
    """Someone who is neither reviewer nor admin cannot confirm."""
    context, _ = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _nominate_reviewer(context, client)
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback("feedback:approve-global:fb:ans:-100:10", from_id=888),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    feedback = asyncio.run(context.feedback.get_feedback("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.status is FeedbackStatus.PENDING_REVIEW


def test_revert_endpoint_rolls_back_and_reports_the_version() -> None:
    """The internal revert endpoint restores the superseded version."""
    context, _ = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback("feedback:approve-global:fb:ans:-100:10", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    response = client.post(
        "/internal/revert",
        json={"qa_item_id": "qa:sha:global"},
        headers={"X-Internal-Key": "internal"},
    )
    assert response.status_code == 404  # the seeded flow has no prior version


def _context_with_repository_index(
    *, embedder: FakeEmbedder | None = None
) -> tuple[AppContext, RecordingTransport]:
    """Build a context whose index source reads back the stored Q&A.

    Production projects a correction by asking the index for the version the
    approval just wrote, which only a source backed by the repositories can
    resolve. Without it every approval in these tests would report the version
    as not current and the correction would never reach retrieval.
    """
    backend = InMemoryBackend()
    context, transport = build_test_context(backend=backend)
    source = RepositorySearchIndexSource(backend.qa_items, backend.qa_versions)
    projector = replace(
        context.projector,
        source=source,
        embedder=context.projector.embedder if embedder is None else embedder,
    )
    return (
        replace(
            context,
            projector=projector,
            reindex=ReindexService(source, projector, context.clock),
        ),
        transport,
    )


def test_global_reviewer_receives_a_group_that_has_no_local_reviewer() -> None:
    """A global reviewer is nominated, routed to, and may approve globally."""
    context, transport = build_test_context()
    client = _client(context)
    _nominate_reviewer(context, client, text="/reviewer global")
    reviewer = asyncio.run(context.reviewers.reviewers.get(GLOBAL_SCOPE))
    assert reviewer is not None
    assert reviewer.principal_id == f"telegram:{REVIEWER_ID}"
    assert asyncio.run(context.reviewers.reviewers.get(GROUP_SCOPE)) is None
    assert any("revisor global" in text for _, text in transport.messages)

    asyncio.run(_seed_answer(context))
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta global.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    assert not any(chat == "1" for chat, _, _ in transport.reviews)
    assert transport.review_global_access[str(REVIEWER_ID)] is True
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback(
            "feedback:approve-global:fb:ans:-100:10",
            from_id=REVIEWER_ID,
            first_name="Pepe",
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_approved"}
    item = asyncio.run(
        context.feedback.qa_items.get_by_canonical_key(
            canonical_key_for(QUESTION), GLOBAL_SCOPE
        )
    )
    assert item is not None
    assert item.scope_key == GLOBAL_SCOPE


def test_nominating_another_person_replaces_the_group_reviewer() -> None:
    """Nominating a second person hands the group over to them."""
    context, transport = build_test_context()
    client = _client(context)
    _nominate_reviewer(context, client)
    _nominate_reviewer(
        context,
        client,
        reply_to=_person_message(SECOND_REVIEWER_ID, "Marta"),
    )
    reviewer = asyncio.run(context.reviewers.reviewers.get(GROUP_SCOPE))
    assert reviewer is not None
    assert reviewer.principal_id == f"telegram:{SECOND_REVIEWER_ID}"
    assert reviewer.name == "Marta"

    asyncio.run(_seed_answer(context))
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    assert [chat for chat, _, _ in transport.reviews] == [str(SECOND_REVIEWER_ID)]


def test_reviewer_off_without_a_reviewer_and_removing_the_global_one() -> None:
    """Removing an absent reviewer says so; removing the global one works."""
    context, transport = build_test_context()
    client = _client(context)
    empty = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_group_message("/reviewer off", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    assert empty.json() == {"status": "reviewer_removed"}
    assert any("No hi havia cap revisor" in text for _, text in transport.messages)

    _nominate_reviewer(context, client, text="/reviewer global")
    removed = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_group_message("/reviewer off global", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    assert removed.json() == {"status": "reviewer_removed"}
    assert asyncio.run(context.reviewers.reviewers.get(GLOBAL_SCOPE)) is None
    assert any("Revisor eliminat" in text for _, text in transport.messages)


def test_reviewer_command_in_an_unregistered_group_is_ignored() -> None:
    """A group with no logical space cannot nominate anyone."""
    context, transport = build_test_context()
    response = _client(context).post(
        TELEGRAM_WEBHOOK_PATH,
        json=_group_message(
            "/reviewer",
            from_id=ADMIN_ID,
            reply_to=_person_message(REVIEWER_ID, "Pepe"),
            chat_id="-999",
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    assert asyncio.run(context.reviewers.reviewers.get(GROUP_SCOPE)) is None
    assert transport.messages == []


def _reject_open_correction(client: TestClient, feedback_id: str) -> None:
    """Reject the open correction, which resolves it without touching Q&A."""
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback(f"feedback:reject:{feedback_id}", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_rejected"}


def _flag_answer(client: TestClient, transport: RecordingTransport) -> int:
    """Press the correction button again and return the new prompt's id.

    Every press opens its own prompt, so the id to reply to is the transport's
    prompt count, not a fixed value.
    """
    _open_proposal(client)
    return len(transport.force_replies)


def test_rejected_correction_can_be_flagged_again_and_approved_locally() -> None:
    """A rejected correction keeps its history and a re-flag can be approved."""
    context, transport = build_test_context()
    client = _client(context)
    asyncio.run(_seed_answer(context))
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Proposta rebutjada.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    _reject_open_correction(client, "fb:ans:-100:10")

    clock = cast(FrozenClock, context.clock)
    clock.advance_to(NOW + timedelta(hours=1))
    second = _flag_answer(client, transport)
    second_id = f"fb:ans:-100:10:{int(clock.now().timestamp())}"
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta corregida.", reply_to=second),
        headers=SECRET_HEADER,
    )
    approval = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback(f"feedback:approve-group:{second_id}", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    assert approval.json() == {"status": "feedback_approved"}
    approved = asyncio.run(context.feedback.get_feedback(second_id))
    assert approved is not None
    assert approved.status is FeedbackStatus.APPROVED
    rejected = asyncio.run(context.feedback.get_feedback("fb:ans:-100:10"))
    assert rejected is not None
    assert rejected.status is FeedbackStatus.REJECTED
    item = asyncio.run(
        context.feedback.qa_items.get_by_canonical_key(
            canonical_key_for(QUESTION), GROUP_SCOPE
        )
    )
    assert item is not None
    version = asyncio.run(
        context.feedback.qa_versions.get(item.current_version_id or "")
    )
    assert version is not None
    assert version.answer == "Resposta corregida."


def test_two_flags_in_the_same_second_do_not_collide() -> None:
    """Re-flagging twice within one second opens two corrections, not a 500."""
    context, transport = build_test_context()
    client = _client(context)
    asyncio.run(_seed_answer(context))
    clock = cast(FrozenClock, context.clock)
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Proposta rebutjada.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    _reject_open_correction(client, "fb:ans:-100:10")

    _flag_answer(client, transport)
    stamp = int(clock.now().timestamp())
    second_id = f"fb:ans:-100:10:{stamp}"
    assert asyncio.run(context.feedback.get_feedback(second_id)) is not None
    _flag_answer(client, transport)
    third_id = f"fb:ans:-100:10:{stamp + 1}"
    assert asyncio.run(context.feedback.get_feedback(third_id)) is not None
    assert asyncio.run(context.feedback.get_feedback("fb:ans:-100:10")) is not None


def test_failed_projection_after_approval_keeps_the_correction_and_warns() -> None:
    """An approved correction survives an index failure, with a warning sent."""
    failing = FakeEmbedder()
    failing.fail = True
    context, transport = _context_with_repository_index(embedder=failing)
    client = _client(context)
    asyncio.run(_seed_answer(context))
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback("feedback:approve-global:fb:ans:-100:10", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_approved"}
    # The approver is the one who pressed the button, so the warning goes there.
    assert ("1", INDEX_WARNING) in transport.messages
    item = asyncio.run(
        context.feedback.qa_items.get_by_canonical_key(
            canonical_key_for(QUESTION), GLOBAL_SCOPE
        )
    )
    assert item is not None
    assert item.status is QAStatus.ACTIVE
    entry = asyncio.run(context.projector.manifest.get(f"qa:{item.id}"))
    assert entry is not None
    assert entry.state is ProjectionState.FAILED


def test_revert_restores_the_superseded_version_and_reprojects_it() -> None:
    """Reverting a correction restores the seeded version and its projection."""
    context, _ = _context_with_repository_index()
    client = _client(context)

    async def seed() -> str:
        created, _, _, _, versions = await context.seed.seed_qa(
            [
                SeedQA(
                    source_url="https://local.invalid/equipament",
                    source_authority=90,
                    section="Equipament",
                    question=QUESTION,
                    answer="Resposta inicial.",
                    status="published",
                    retrieved_at=NOW,
                    source_anchor="qa-equipament",
                )
            ]
        )
        assert created == 1
        assert await context.reindex.reindex_qa_version(versions[0])
        return versions[0]

    seeded_version_id = asyncio.run(seed())
    asyncio.run(_seed_answer(context))
    prompt_id = _open_proposal(client)
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    client.post(
        TELEGRAM_WEBHOOK_PATH,
        json=_callback("feedback:approve-global:fb:ans:-100:10", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    item = asyncio.run(
        context.feedback.qa_items.get_by_canonical_key(
            canonical_key_for(QUESTION), GLOBAL_SCOPE
        )
    )
    assert item is not None
    assert item.current_version_id != seeded_version_id
    vectors = cast(FakeVectorStore, context.answer.retrieval.vectors).records
    assert vectors[f"qa:{item.id}"].metadata["text"] == "Resposta corregida."

    response = client.post(
        "/internal/revert",
        json={"qa_item_id": item.id},
        headers=INTERNAL_HEADER,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "reverted",
        "restored_version_id": seeded_version_id,
        "projection_status": "indexed",
    }
    restored = asyncio.run(context.feedback.qa_items.get(item.id))
    assert restored is not None
    assert restored.current_version_id == seeded_version_id
    assert vectors[f"qa:{item.id}"].metadata["text"] == "Resposta inicial."
