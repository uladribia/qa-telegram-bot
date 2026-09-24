# SPDX-License-Identifier: MIT
"""Tests for the authority, trigger, and recap policies."""

from datetime import UTC, datetime, timedelta

from knowledge_bot.domain.policies import (
    is_addressed,
    is_ask_command,
    is_recap_due,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _hours(count: int) -> timedelta:
    return timedelta(hours=count)


def test_bare_question_is_not_addressed() -> None:
    """A question-shaped message alone does not trigger a reply."""
    assert is_addressed("Quan entrenen?") is False


def test_addressing_signals_trigger_a_reply() -> None:
    """Mention, reply-to-bot, direct message, and /ask are triggers."""
    assert is_addressed("quan entrenen?", mentions_bot=True) is True
    assert is_addressed("quan entrenen?", is_reply_to_bot=True) is True
    assert is_addressed("quan entrenen?", is_direct_message=True) is True
    assert is_addressed("/ask quan entrenen?") is True
    assert is_addressed("/ask@bhc_qa_testbot quan entrenen?") is True


def test_ask_command_detection() -> None:
    """Only the /ask command is recognised as a command trigger."""
    assert is_ask_command("/ask") is True
    assert is_ask_command("  /ask@bot hola") is True
    assert is_ask_command("/askme") is False
    assert is_ask_command("hola") is False
    assert is_ask_command(None) is False


def test_recap_due_policy() -> None:
    """A recap is due when never sent or once the interval has elapsed."""
    assert is_recap_due(None, NOW) is True
    assert is_recap_due(NOW - _hours(23), NOW, interval_hours=24) is False
    assert is_recap_due(NOW - _hours(25), NOW, interval_hours=24) is True
