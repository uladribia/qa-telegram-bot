# SPDX-License-Identifier: MIT
"""Tests for the authority, trigger, and recap policies."""

from datetime import UTC, datetime, timedelta

from knowledge_bot.domain.enums import QAOrigin, SourceType
from knowledge_bot.domain.policies import (
    Authority,
    highest_authority,
    is_addressed,
    is_ask_command,
    is_decisive_alone,
    is_recap_due,
    origin_authority,
    source_authority,
    telegram_authority,
    web_seed_authority,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _hours(count: int) -> timedelta:
    return timedelta(hours=count)


def test_in_review_is_not_decisive_alone() -> None:
    """A single in-review web entry cannot establish a fact."""
    assert not is_decisive_alone(Authority.WEB_IN_REVIEW)
    assert is_decisive_alone(Authority.WEB_PUBLISHED)


def test_admin_approval_dominates() -> None:
    """Admin-approved knowledge outranks every other source."""
    assert (
        highest_authority([Authority.TELEGRAM_USER, Authority.ADMIN_APPROVED])
        is Authority.ADMIN_APPROVED
    )


def test_highest_authority_of_empty_list_is_none() -> None:
    """There is no authority without evidence."""
    assert highest_authority([]) is None


def test_telegram_authority_depends_on_admin_flag() -> None:
    """Admin messages outrank regular member messages."""
    assert telegram_authority(is_admin=True) is Authority.TELEGRAM_ADMIN
    assert telegram_authority(is_admin=False) is Authority.TELEGRAM_USER


def test_web_seed_authority_reflects_review_status() -> None:
    """In-review web entries rank below published ones."""
    assert web_seed_authority(in_review=True) is Authority.WEB_IN_REVIEW
    assert web_seed_authority(in_review=False) is Authority.WEB_PUBLISHED


def test_source_and_origin_authority_mapping() -> None:
    """Baseline authority matches the spec table."""
    assert source_authority(SourceType.WHATSAPP_IMPORT) is Authority.WHATSAPP_IMPORT
    assert source_authority(SourceType.TELEGRAM) is Authority.TELEGRAM_USER
    assert origin_authority(QAOrigin.ADMIN_APPROVED) is Authority.ADMIN_APPROVED
    assert origin_authority(QAOrigin.AUTO_GENERATED) is Authority.AUTO_GENERATED


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
