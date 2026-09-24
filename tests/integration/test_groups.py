# SPDX-License-Identifier: MIT
"""Integration tests for channel-independent space binding."""

from datetime import UTC, datetime

import pytest

from knowledge_bot.application.groups import SpaceDirectory
from tests.fakes.repositories import (
    InMemoryChannelBindingRepository,
    InMemoryConversationRepository,
    InMemorySourceRepository,
    InMemorySpaceRepository,
)
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)


def _directory() -> SpaceDirectory:
    """Return a directory wired to in-memory fakes."""
    return SpaceDirectory(
        sources=InMemorySourceRepository(),
        conversations=InMemoryConversationRepository(),
        spaces=InMemorySpaceRepository(),
        bindings=InMemoryChannelBindingRepository(),
        clock=FrozenClock(NOW),
    )


async def test_bind_accepts_connector_declared_source_identity() -> None:
    """The directory does not know or infer the connector's source kind."""
    directory = _directory()
    space_id = await directory.bind(
        channel="custom-chat",
        external_conversation_id="room-1",
        conversation_id="conversation-1",
        space_id=None,
        title="Example",
        source_id="src:custom:runtime",
        source_kind="custom_messages",
        source_authority=42,
    )
    binding = await directory.resolve("custom-chat", "room-1")
    assert binding is not None
    assert binding.space_id == space_id
    assert binding.conversation_id == "conversation-1"
    source = await directory.sources.get("src:custom:runtime")
    assert source is not None
    assert source.source_type == "custom_messages"
    assert source.authority == 42


async def test_bind_is_idempotent_and_refreshes_title() -> None:
    """Re-binding an external conversation only updates its metadata."""
    directory = _directory()
    first = await directory.bind(
        channel="custom-chat",
        external_conversation_id="room-1",
        conversation_id="conversation-1",
        space_id=None,
        title="Example",
        source_id="src:custom:runtime",
        source_kind="custom_messages",
        source_authority=42,
    )
    second = await directory.bind(
        channel="custom-chat",
        external_conversation_id="room-1",
        conversation_id="conversation-1",
        space_id=first,
        title="Renamed",
        source_id="src:custom:runtime",
        source_kind="custom_messages",
        source_authority=42,
    )
    assert second == first
    binding = await directory.resolve("custom-chat", "room-1")
    assert binding is not None and binding.title == "Renamed"
    conversation = await directory.conversations.get("conversation-1")
    assert conversation is not None and conversation.title == "Renamed"


async def test_bind_rejects_empty_external_id() -> None:
    """An empty external conversation id is a configuration error."""
    directory = _directory()
    with pytest.raises(ValueError):
        await directory.bind(
            channel="custom-chat",
            external_conversation_id="  ",
            conversation_id="conversation-1",
            space_id=None,
            title=None,
            source_id="src:custom:runtime",
            source_kind="custom_messages",
            source_authority=42,
        )
