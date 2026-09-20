# SPDX-License-Identifier: MIT
"""Integration tests for group registration (scope plumbing)."""

from datetime import UTC, datetime

import pytest

from knowledge_bot.application.groups import GroupRegistrar
from knowledge_bot.domain.enums import SourceType
from tests.fakes.repositories import (
    InMemoryConversationRepository,
    InMemorySourceRepository,
)
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)


def _registrar() -> tuple[GroupRegistrar, InMemoryConversationRepository]:
    """Return a registrar wired to in-memory fakes."""
    sources = InMemorySourceRepository()
    conversations = InMemoryConversationRepository()
    return (
        GroupRegistrar(
            sources=sources,
            conversations=conversations,
            clock=FrozenClock(NOW),
        ),
        conversations,
    )


async def test_register_creates_conversation_under_telegram_source() -> None:
    """A new group is a conversation under the Telegram runtime source."""
    registrar, conversations = _registrar()
    created = await registrar.register("-100", title="Prebenjamins")
    assert created is True
    conversation = await conversations.get("-100")
    assert conversation is not None
    assert conversation.source_id == SourceType.TELEGRAM.value
    assert conversation.title == "Prebenjamins"


async def test_register_is_idempotent_and_refreshes_title() -> None:
    """Re-registering an existing group only updates its title."""
    registrar, conversations = _registrar()
    await registrar.register("-100", title="Prebenjamins")
    created = await registrar.register("-100", title="Prebenjamins A")
    assert created is False
    conversation = await conversations.get("-100")
    assert conversation is not None
    assert conversation.title == "Prebenjamins A"


async def test_register_rejects_empty_chat_id() -> None:
    """An empty chat id is a configuration error."""
    registrar, _ = _registrar()
    with pytest.raises(ValueError):
        await registrar.register("  ")
