# SPDX-License-Identifier: MIT
"""Decide what to do with an inbound message.

Answering and listening are separate decisions:

- An addressed message is always ingested and answered.
- An unaddressed message is ingested only when the background listener is
  enabled, and is never answered.

The background listener is off by default so the bot stays silent unless asked.
"""

from enum import StrEnum

from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.domain.policies import is_addressed


class IntakeAction(StrEnum):
    """What the intake pipeline should do with a message."""

    IGNORE = "ignore"
    INGEST = "ingest"
    ANSWER = "answer"


def decide_intake(
    message: NormalizedMessage,
    *,
    background_listener_enabled: bool = False,
) -> IntakeAction:
    """Decide whether to ignore, ingest, or answer a message.

    Args:
        message: The normalized inbound message.
        background_listener_enabled: Whether unaddressed traffic is ingested.

    Returns:
        ``ANSWER`` when addressed, ``INGEST`` when listening, else ``IGNORE``.
    """
    addressed = is_addressed(
        message.text,
        mentions_bot=message.mentions_bot,
        is_reply_to_bot=message.is_reply_to_bot,
        is_direct_message=message.is_direct_message,
    )
    if addressed:
        return IntakeAction.ANSWER
    if background_listener_enabled:
        return IntakeAction.INGEST
    return IntakeAction.IGNORE
