# SPDX-License-Identifier: MIT
"""Integration tests for reviewer nomination and routing over the webhook."""

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from knowledge_bot.adapters.http.app import create_app
from knowledge_bot.application.feedback import PROPOSAL_ACK, PROPOSAL_PROMPT
from knowledge_bot.domain.entities import BotAnswer
from knowledge_bot.domain.enums import AnswerMode, FeedbackStatus
from knowledge_bot.domain.scope import scope_for_space
from knowledge_bot.infrastructure.composition import AppContext
from tests.fakes.context import (
    SPACE_A,
    WEBHOOK_SECRET,
    build_test_context,
)

SECRET_HEADER = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
NOW = datetime(2026, 9, 21, 18, 4, tzinfo=UTC)
REVIEWER_ID = 222
ADMIN_ID = 1
GROUP_SCOPE = scope_for_space(SPACE_A)


def _client(context: AppContext) -> TestClient:
    return TestClient(create_app(lambda request: context))


def _group_message(
    text: str,
    *,
    from_id: int,
    reply_to: dict[str, object] | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": 50,
        "date": 1789000000,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": from_id, "is_bot": False},
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = reply_to
    return {"update_id": 5, "message": message}


def _pepe_message() -> dict[str, object]:
    return {
        "message_id": 40,
        "date": 1789000000,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {
            "id": REVIEWER_ID,
            "is_bot": False,
            "first_name": "Pepe",
        },
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


def _nominate_reviewer(context: AppContext, client: TestClient) -> None:
    response = client.post(
        "/telegram/webhook",
        json=_group_message(
            "/reviewer",
            from_id=ADMIN_ID,
            reply_to=_pepe_message(),
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "reviewer_nominated"}


def _open_proposal(client: TestClient) -> int:
    """Start a correction and return the prompt message id to reply to."""
    response = client.post(
        "/telegram/webhook",
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
    assert reviewer.user_id == str(REVIEWER_ID)
    assert reviewer.name == "Pepe"
    assert any("revisor d'aquest grup" in text for _, text in transport.messages)


def test_non_admin_cannot_nominate() -> None:
    """A nomination from anyone but the admin is ignored."""
    context, _ = build_test_context()
    response = _client(context).post(
        "/telegram/webhook",
        json=_group_message("/reviewer", from_id=777, reply_to=_pepe_message()),
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
        "/telegram/webhook",
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
        "/telegram/webhook",
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
        "/telegram/webhook",
        json=_callback("feedback:start:ans:-100:10", from_id=777, first_name="Joana"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_started"}
    feedback = asyncio.run(context.feedback_repo.get("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.reporter_chat_id == "777"
    assert len(transport.force_replies) == 2


def test_flag_prompt_fails_with_an_alert_when_dm_is_unreachable() -> None:
    """A presser who never started the bot gets an alert, not silence."""
    context, transport = build_test_context()
    transport.dead_chats = frozenset({"555"})
    asyncio.run(_seed_answer(context))
    response = _client(context).post(
        "/telegram/webhook",
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
    feedback = asyncio.run(context.feedback_repo.get("fb:ans:-100:10"))
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
        "/telegram/webhook",
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    assert all(chat != str(REVIEWER_ID) for chat, _, _ in transport.reviews)
    assert not any(chat == "1" for chat, _, _ in transport.reviews)
    assert any(
        chat == "1" and "continua pendent" in text for chat, text in transport.messages
    )
    assert any(chat == "-100" and "@Pepe" in text for chat, text in transport.messages)


def test_reviewer_off_removes_the_group_reviewer() -> None:
    """`/reviewer off` clears the group's reviewer."""
    context, _ = build_test_context()
    client = _client(context)
    _nominate_reviewer(context, client)
    response = client.post(
        "/telegram/webhook",
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
        "/telegram/webhook",
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


def test_group_reviewer_can_confirm_and_admin_gets_a_report() -> None:
    """The group's reviewer approves; the admin receives the always-mode report."""
    context, transport = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _nominate_reviewer(context, client)
    prompt_id = _open_proposal(client)
    client.post(
        "/telegram/webhook",
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    response = client.post(
        "/telegram/webhook",
        json=_callback(
            "feedback:approve-group:fb:ans:-100:10",
            from_id=REVIEWER_ID,
            first_name="Pepe",
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_approved"}
    feedback = asyncio.run(context.feedback_repo.get("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.status is FeedbackStatus.APPROVED
    report = [text for chat, text in transport.messages if chat == "1"]
    assert any("Correccions revisades" in text for text in report)
    assert any("Pepe" in text for text in report)


def test_local_reviewer_global_approval_is_denied_by_server() -> None:
    """A forged global callback cannot bypass authorization."""
    context, _ = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _nominate_reviewer(context, client)
    prompt_id = _open_proposal(client)
    client.post(
        "/telegram/webhook",
        json=_reply("Resposta global.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    response = client.post(
        "/telegram/webhook",
        json=_callback(
            "feedback:approve-global:fb:ans:-100:10",
            from_id=REVIEWER_ID,
        ),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    feedback = asyncio.run(context.feedback_repo.get("fb:ans:-100:10"))
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
        "/telegram/webhook",
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    response = client.post(
        "/telegram/webhook",
        json=_callback("feedback:approve-global:fb:ans:-100:10", from_id=888),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    feedback = asyncio.run(context.feedback_repo.get("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.status is FeedbackStatus.PENDING_REVIEW


def test_batch_mode_sends_the_report_only_when_poked() -> None:
    """In batch mode the admin report waits for the interval check."""
    context, transport = build_test_context(admin_report_mode="batch")
    asyncio.run(_seed_answer(context))
    client = _client(context)
    _nominate_reviewer(context, client)
    prompt_id = _open_proposal(client)
    client.post(
        "/telegram/webhook",
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    client.post(
        "/telegram/webhook",
        json=_callback("feedback:approve-group:fb:ans:-100:10", from_id=REVIEWER_ID),
        headers=SECRET_HEADER,
    )
    assert not any("Correccions revisades" in text for _, text in transport.messages)
    events = asyncio.run(context.reviewer_report.events.list_unreported())
    assert len(events) == 1
    assert events[0].created_at == context.clock.now()
    response = client.post(
        "/internal/report",
        headers={"X-Internal-Key": "internal"},
    )
    assert response.json() == {"status": "sent"}
    assert any(
        chat == "1" and "Correccions revisades" in text
        for chat, text in transport.messages
    )


def test_revert_endpoint_rolls_back_and_reports_the_version() -> None:
    """The internal revert endpoint restores the superseded version."""
    context, _ = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    prompt_id = _open_proposal(client)
    client.post(
        "/telegram/webhook",
        json=_reply("Resposta corregida.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    client.post(
        "/telegram/webhook",
        json=_callback("feedback:approve-global:fb:ans:-100:10", from_id=ADMIN_ID),
        headers=SECRET_HEADER,
    )
    response = client.post(
        "/internal/revert",
        json={"qa_item_id": "qa:sha:global"},
        headers={"X-Internal-Key": "internal"},
    )
    assert response.status_code == 404  # the seeded flow has no prior version
