# SPDX-License-Identifier: MIT
"""Telegram outbound transport."""

from knowledge_bot.ports.http import HttpClient

# Inline button shown under every answer (spec §21).
FEEDBACK_BUTTON = "\u26a0\ufe0f Est\u00e0 malament?"


def feedback_keyboard(answer_id: str) -> dict[str, object]:
    """Build the inline keyboard carrying the feedback button.

    Args:
        answer_id: The stored bot answer id.

    Returns:
        A Telegram ``reply_markup`` payload.
    """
    return {
        "inline_keyboard": [
            [{"text": FEEDBACK_BUTTON, "callback_data": f"feedback:start:{answer_id}"}]
        ]
    }


def review_keyboard(feedback_id: str) -> dict[str, object]:
    """Build the admin review buttons for a correction.

    Args:
        feedback_id: The correction under review.

    Returns:
        A Telegram ``reply_markup`` payload.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": "\u2705 Aprovar",
                    "callback_data": f"feedback:approve:{feedback_id}",
                },
                {
                    "text": "\u270f\ufe0f Editar",
                    "callback_data": f"feedback:edit:{feedback_id}",
                },
                {
                    "text": "\u274c Rebutjar",
                    "callback_data": f"feedback:reject:{feedback_id}",
                },
            ]
        ]
    }


class TelegramTransport:
    """Send and edit messages through the Telegram Bot API."""

    def __init__(
        self,
        http: HttpClient,
        bot_token: str,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        """Create the transport.

        Args:
            http: The HTTP client used to call Telegram.
            bot_token: The Telegram bot token.
            api_base: Base URL of the Telegram Bot API.
        """
        self._http = http
        self._token = bot_token
        self._api_base = api_base

    async def _post(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        return await self._http.post_json(
            f"{self._api_base}/bot{self._token}/{method}", payload
        )

    @staticmethod
    def _message_id(response: dict[str, object]) -> str | None:
        result = response.get("result")
        if isinstance(result, dict):
            message_id = result.get("message_id")
            return str(message_id) if message_id is not None else None
        return None

    async def send_message(self, conversation_id: str, text: str) -> str | None:
        """Send a plain text message and return the Telegram message id."""
        response = await self._post(
            "sendMessage", {"chat_id": conversation_id, "text": text}
        )
        return self._message_id(response)

    async def send_answer(
        self, conversation_id: str, text: str, answer_id: str
    ) -> str | None:
        """Send an answer carrying the feedback button for an answer id.

        Args:
            conversation_id: The conversation to send to.
            text: The answer text.
            answer_id: The stored bot answer id the feedback button refers to.

        Returns:
            The Telegram message id, if available.
        """
        response = await self._post(
            "sendMessage",
            {
                "chat_id": conversation_id,
                "text": text,
                "reply_markup": feedback_keyboard(answer_id),
            },
        )
        return self._message_id(response)

    async def edit_message(
        self,
        conversation_id: str,
        message_id: str,
        text: str,
    ) -> bool:
        """Replace the text of a message."""
        response = await self._post(
            "editMessageText",
            {"chat_id": conversation_id, "message_id": message_id, "text": text},
        )
        return bool(response.get("ok", True))

    async def send_review(
        self, conversation_id: str, text: str, feedback_id: str
    ) -> str | None:
        """Send an admin review message with approve/edit/reject buttons.

        Args:
            conversation_id: The admin's private chat.
            text: The review text.
            feedback_id: The correction under review.

        Returns:
            The Telegram message id, if available.
        """
        response = await self._post(
            "sendMessage",
            {
                "chat_id": conversation_id,
                "text": text,
                "reply_markup": review_keyboard(feedback_id),
            },
        )
        return self._message_id(response)

    async def send_force_reply(self, conversation_id: str, text: str) -> str | None:
        """Send a message that asks the user to reply, returning its message id."""
        response = await self._post(
            "sendMessage",
            {
                "chat_id": conversation_id,
                "text": text,
                "reply_markup": {"force_reply": True, "selective": True},
            },
        )
        return self._message_id(response)

    async def answer_callback(self, callback_id: str) -> None:
        """Acknowledge an inline-button press."""
        await self._post("answerCallbackQuery", {"callback_query_id": callback_id})
