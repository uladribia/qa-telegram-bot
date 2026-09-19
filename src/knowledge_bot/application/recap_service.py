# SPDX-License-Identifier: MIT
"""Opportunistic recap service.

Python Workers have no cron handler, so the recap is sent opportunistically:
whenever an event arrives, the service checks whether the recap is due and posts
it at most once per interval.
"""

from dataclasses import dataclass
from datetime import timedelta

from knowledge_bot.application.recap import (
    DEFAULT_RECAP_LANGUAGE,
    build_recap,
    render_recap,
)
from knowledge_bot.domain.policies import is_recap_due
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import BotAnswerRepository, RecapStateRepository
from knowledge_bot.ports.transport import MessageTransport


@dataclass(frozen=True, slots=True)
class RecapService:
    """Send the periodic recap opportunistically."""

    answers: BotAnswerRepository
    state: RecapStateRepository
    transport: MessageTransport
    clock: Clock
    enabled: bool = True
    interval_hours: int = 24
    language: str = DEFAULT_RECAP_LANGUAGE

    async def maybe_send(self, conversation_id: str) -> bool:
        """Send the recap when it is due.

        Args:
            conversation_id: The conversation to recap and post to.

        Returns:
            ``True`` when a recap was sent, ``False`` when it was disabled or
            not yet due.
        """
        if not self.enabled:
            return False
        now = self.clock.now()
        if not is_recap_due(
            self.state.get_last_sent_at(conversation_id),
            now,
            interval_hours=self.interval_hours,
        ):
            return False
        window_start = now - timedelta(hours=self.interval_hours)
        recap = build_recap(
            self.answers.list_between(window_start, now),
            window_start=window_start,
            window_end=now,
        )
        await self.transport.send_message(
            conversation_id,
            render_recap(recap, language=self.language),
        )
        self.state.set_last_sent_at(conversation_id, now)
        return True
