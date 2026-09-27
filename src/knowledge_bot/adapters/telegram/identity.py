# SPDX-License-Identifier: MIT
"""Telegram identity: who the bot is, and who may talk to it privately.

These identifiers are Telegram-specific by construction (they are raw Bot API
user ids), so they live with the connector rather than in shared application
context.
"""

import hashlib
from dataclasses import dataclass

TELEGRAM_RUNTIME_SOURCE_ID = "src:telegram:runtime"


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    """Identifiers the adapter needs to route and address messages.

    ``allowed_user_ids`` lists the people who may open a private chat with the
    bot. The admin is always allowed implicitly.
    """

    admin_user_id: str | None = None
    bot_id: str | None = None
    bot_username: str | None = None
    allowed_user_ids: frozenset[str] = frozenset()

    def allows_sender(self, user_id: str | None) -> bool:
        """Return whether a user may start a private conversation.

        Args:
            user_id: The raw Telegram user id, if known.

        Returns:
            ``True`` for the admin and for anyone on the allowed list.
        """
        if user_id is None:
            return False
        if self.admin_user_id and user_id == self.admin_user_id:
            return True
        return user_id in self.allowed_user_ids


def pseudonymize(value: str) -> str:
    """Return a stable, non-reversible identifier for a user.

    Args:
        value: The raw identifier (for example a Telegram user id).

    Returns:
        A short hexadecimal hash.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
