# SPDX-License-Identifier: MIT
"""Integration tests for the feedback flow over the webhook (spec §34)."""

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from knowledge_bot.adapters.inbound.fastapi_routes import create_app
from knowledge_bot.adapters.outbound.telegram import FEEDBACK_BUTTON
from knowledge_bot.application.feedback import (
    PROPOSAL_ACK,
    PROPOSAL_PROMPT,
    render_review,
)
from knowledge_bot.domain.entities import BotAnswer
from knowledge_bot.domain.enums import AnswerMode, FeedbackStatus
from knowledge_bot.infrastructure.composition import AppContext
from tests.fakes.context import WEBHOOK_SECRET, build_test_context

SECRET_HEADER = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)


def _client(context: AppContext) -> TestClient:
    return TestClient(create_app(lambda request: context))


def _callback(data: str, *, from_id: int = 555) -> dict[str, object]:
    return {
        "update_id": 7,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": from_id, "is_bot": False},
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
            "message_id": 50,
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
            question="Com es demana l'equipament?",
            answer="Resposta antiga.",
            answer_mode=AnswerMode.DIRECT_QA,
            created_at=NOW,
            user_message_id="-100:10",
        )
    )


def test_button_press_prompts_the_reporter_privately() -> None:
    """Pressing the button opens a private proposal prompt, not a group one."""
    context, transport = build_test_context()
    asyncio.run(_seed_answer(context))
    response = _client(context).post(
        "/telegram/webhook",
        json=_callback("feedback:start:ans:-100:10"),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_started"}
    assert transport.force_replies == [("555", PROPOSAL_PROMPT)]
    assert not any(
        chat == "555" and "group" in text for chat, text in transport.messages
    )


def test_proposal_goes_to_the_admin_dm_and_acks_the_reporter() -> None:
    """The proposal is acked to the reporter and reviewed in the admin DM."""
    context, transport = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    client.post(
        "/telegram/webhook",
        json=_callback("feedback:start:ans:-100:10"),
        headers=SECRET_HEADER,
    )
    prompt_id = transport.force_replies.__len__()
    response = client.post(
        "/telegram/webhook",
        json=_reply("La llista la passa l'entrenador.", reply_to=prompt_id),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "proposed"}
    assert ("555", PROPOSAL_ACK) in transport.messages
    admin_reviews = [text for chat, text, _ in transport.reviews if chat == "1"]
    assert any("Correcci\u00f3 proposada" in text for text in admin_reviews)


def test_approve_creates_a_version_and_thanks_the_reporter() -> None:
    """Approving supersedes the version and thanks the reporter privately."""
    context, transport = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    client.post(
        "/telegram/webhook",
        json=_callback("feedback:start:ans:-100:10"),
        headers=SECRET_HEADER,
    )
    client.post(
        "/telegram/webhook",
        json=_reply("Resposta corregida.", reply_to=1),
        headers=SECRET_HEADER,
    )
    response = client.post(
        "/telegram/webhook",
        json=_callback("feedback:approve:fb:ans:-100:10", from_id=1),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_approved"}
    feedback = asyncio.run(context.feedback.get("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.status is FeedbackStatus.APPROVED
    assert any(
        "Gr\u00e0cies" in text for chat, text in transport.messages if chat == "555"
    )


def test_reject_keeps_the_old_answer() -> None:
    """Rejecting leaves knowledge untouched and confirms to the admin."""
    context, _ = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    client.post(
        "/telegram/webhook",
        json=_callback("feedback:start:ans:-100:10"),
        headers=SECRET_HEADER,
    )
    client.post(
        "/telegram/webhook", json=_reply("proposta", reply_to=1), headers=SECRET_HEADER
    )
    response = client.post(
        "/telegram/webhook",
        json=_callback("feedback:reject:fb:ans:-100:10", from_id=1),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "feedback_rejected"}
    feedback = asyncio.run(context.feedback.get("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.status is FeedbackStatus.REJECTED


def test_non_admin_cannot_confirm() -> None:
    """A group user cannot approve or reject; only the admin can."""
    context, _ = build_test_context()
    asyncio.run(_seed_answer(context))
    client = _client(context)
    client.post(
        "/telegram/webhook",
        json=_callback("feedback:start:ans:-100:10"),
        headers=SECRET_HEADER,
    )
    client.post(
        "/telegram/webhook", json=_reply("proposta", reply_to=1), headers=SECRET_HEADER
    )
    response = client.post(
        "/telegram/webhook",
        json=_callback("feedback:approve:fb:ans:-100:10", from_id=555),
        headers=SECRET_HEADER,
    )
    assert response.json() == {"status": "ignored"}
    feedback = asyncio.run(context.feedback.get("fb:ans:-100:10"))
    assert feedback is not None
    assert feedback.status is FeedbackStatus.PENDING_ADMIN


def test_unknown_callback_is_ignored() -> None:
    """A callback that matches no action is ignored."""
    context, _ = build_test_context()
    response = _client(context).post(
        "/telegram/webhook", json=_callback("nonsense"), headers=SECRET_HEADER
    )
    assert response.json() == {"status": "ignored"}


def test_review_text_includes_current_and_proposed() -> None:
    """The review payload shows the current and proposed answers."""
    from knowledge_bot.application.feedback import CorrectionRequest

    text = render_review(
        CorrectionRequest(
            feedback_id="fb:1",
            question="Com?",
            current_answer="Antiga",
            proposed_answer="Nova",
        )
    )
    assert "Antiga" in text
    assert "Nova" in text
    assert "Com?" in text


def test_button_label_is_the_expected_catalan() -> None:
    """The button label matches the plan."""
    assert FEEDBACK_BUTTON == "\u26a0\ufe0f Est\u00e0 malament?"
