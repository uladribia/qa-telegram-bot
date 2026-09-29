# SPDX-License-Identifier: MIT
"""Tests for the Telegram outbound transport."""

from typing import cast

import pytest

from knowledge_bot.adapters.outbound.telegram import (
    TelegramTransport,
    feedback_keyboard,
    review_keyboard,
)
from knowledge_bot.application.answer_question import (
    addressed_answer_id,
    proactive_answer_id,
    scoped_answer_id,
)
from tests.fakes.http import RecordingHttpClient


async def test_send_message_posts_to_telegram() -> None:
    """The transport posts sendMessage with the chat and text."""
    http = RecordingHttpClient()
    transport = TelegramTransport(http, "TOKEN")
    await transport.send_message("-100", "hola")
    assert http.calls == [
        (
            "https://api.telegram.org/botTOKEN/sendMessage",
            {"chat_id": "-100", "text": "hola"},
        )
    ]


def test_local_reviewer_keyboard_omits_global_approval() -> None:
    """A local reviewer sees only the local approval action."""
    keyboard = review_keyboard("fb-1", include_global=False)
    inline_keyboard = keyboard.get("inline_keyboard")
    assert isinstance(inline_keyboard, list)
    first_row = inline_keyboard[0]
    assert isinstance(first_row, list)
    callbacks = [
        cast("dict[str, object]", button).get("callback_data") for button in first_row
    ]
    assert callbacks == ["feedback:approve-group:fb-1"]


async def test_telegram_ok_false_is_a_delivery_failure() -> None:
    """A Telegram-level error is not mistaken for a delivered message."""
    http = RecordingHttpClient()
    http.responses.append({"ok": False, "result": {"message_id": 99}})
    transport = TelegramTransport(http, "TOKEN")

    assert await transport.send_message("-100", "hola") is None

    http.responses.append({"ok": False, "result": {"message_id": 99}})
    assert await transport.edit_message("-100", "99", "editat") is False


#: Telegram rejects a message whose ``callback_data`` exceeds this, with a 400
#: and no delivery. The cap is on the encoded payload, not on our id.
CALLBACK_DATA_LIMIT = 64

#: The widest chat id and message id Telegram hands out, so the budget is
#: pinned against the worst case rather than today's fixtures.
WIDEST_MESSAGE_ID = f"-1009999999999:{999999999}"


def _callback_data(answer_id: str) -> bytes:
    """Return the bytes Telegram would carry for an answer's feedback button."""
    keyboard = feedback_keyboard(answer_id)
    rows = cast(list[list[dict[str, str]]], keyboard["inline_keyboard"])
    return rows[0][0]["callback_data"].encode()


@pytest.mark.parametrize(
    "answer_id",
    [
        addressed_answer_id(WIDEST_MESSAGE_ID),
        scoped_answer_id(WIDEST_MESSAGE_ID, None),
        scoped_answer_id(WIDEST_MESSAGE_ID, "-1009999999999"),
        proactive_answer_id(WIDEST_MESSAGE_ID, "-1009999999999"),
    ],
)
def test_every_answer_id_fits_the_feedback_button(answer_id: str) -> None:
    """An answer whose button Telegram refuses is an answer nobody receives.

    The ids carry their scope so a replay cannot reuse another scope's answer,
    and that made them long enough to break delivery without anyone noticing:
    the answer was persisted, and the send silently failed on a 400.
    """
    assert len(_callback_data(answer_id)) <= CALLBACK_DATA_LIMIT


def test_a_space_id_is_too_long_to_be_an_answer_id() -> None:
    """The reason the id takes a chat reference and not the space id.

    A space id is 35 characters; with the prefix and a realistic message id it
    does not fit, which is exactly how the multi-scope private answer stopped
    being delivered.
    """
    space_id = "sp_" + "0" * 32
    assert len(_callback_data(scoped_answer_id("101605540:480", space_id))) > (
        CALLBACK_DATA_LIMIT
    )
