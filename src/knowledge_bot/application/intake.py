# SPDX-License-Identifier: MIT
"""Decide what to do with an inbound message.

One conversation has one mode, and the mode is cumulative: how much the bot
does when nobody addressed it.

- ``off`` ignores the message, but the conversation still resolves, so
  membership is observed and the control plane keeps working.
- ``silent`` stores and indexes everything and answers nothing.
- ``active`` answers what is addressed and stores the rest.
- ``proactive`` also answers a confident unaddressed question.

Whether the message addresses the bot is a property of the message, never of
the mode: a ``proactive`` group answers a mention exactly as an ``active`` one
does.
"""

from enum import StrEnum

from knowledge_bot.domain.enums import BotMode
from knowledge_bot.domain.policies import is_addressed
from knowledge_bot.models.messages import NormalizedMessage


class IntakeAction(StrEnum):
    """What the intake pipeline should do with a message."""

    IGNORE = "ignore"
    INGEST = "ingest"
    ANSWER = "answer"
    #: Store it, then try to answer it without being asked. Only a proactive
    #: conversation produces it, and only a confident question survives the
    #: listener that ran first.
    PROACTIVE = "proactive"


def is_addressed_to_bot(message: NormalizedMessage) -> bool:
    """Return whether the message explicitly addresses the bot.

    Args:
        message: The normalized inbound message.

    Returns:
        ``True`` for a mention, a reply to the bot, a direct message, or ``/ask``.
    """
    return is_addressed(
        message.text,
        mentions_bot=message.mentions_bot,
        is_reply_to_bot=message.is_reply_to_bot,
        is_direct_message=message.is_direct_message,
    )


def decide_intake(message: NormalizedMessage, mode: BotMode) -> IntakeAction:
    """Decide whether to ignore, ingest, answer, or proactively answer.

    Args:
        message: The normalized inbound message.
        mode: The bot mode of the conversation the message landed in.

    Returns:
        The action the conversation's mode allows for this message.
    """
    if mode is BotMode.OFF:
        return IntakeAction.IGNORE
    if mode is BotMode.SILENT:
        return IntakeAction.INGEST
    if is_addressed_to_bot(message):
        return IntakeAction.ANSWER
    if mode is BotMode.PROACTIVE:
        return IntakeAction.PROACTIVE
    return IntakeAction.INGEST
