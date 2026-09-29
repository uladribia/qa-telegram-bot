# SPDX-License-Identifier: MIT
"""Integration tests for channel-independent space binding."""

from datetime import UTC, datetime

import pytest

from knowledge_bot.application.groups import MembershipDirectory, SpaceDirectory
from knowledge_bot.domain.enums import BotMode
from tests.fakes.repositories import (
    InMemoryChannelBindingRepository,
    InMemoryConversationRepository,
    InMemorySourceRepository,
    InMemorySpaceMembershipRepository,
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


async def _bind(
    directory: SpaceDirectory,
    *,
    room: str = "room-1",
    title: str | None = None,
    space_id: str | None = None,
    bot_mode: BotMode | None = None,
) -> str:
    """Bind one external conversation and return its space id."""
    return await directory.bind(
        channel="custom-chat",
        external_conversation_id=room,
        conversation_id=room,
        space_id=space_id,
        title=title,
        source_id="src:custom:runtime",
        source_kind="custom_messages",
        source_authority=42,
        bot_mode=bot_mode,
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


async def test_changing_the_mode_keeps_the_name() -> None:
    """A mode-only update must not blank the group it is applied to."""
    directory = _directory()
    space_id = await _bind(directory, title="Example")
    await _bind(directory, space_id=space_id, title=None, bot_mode=BotMode.PROACTIVE)
    binding = await directory.resolve("custom-chat", "room-1")
    assert binding is not None
    assert binding.bot_mode is BotMode.PROACTIVE
    assert binding.title == "Example"
    conversation = await directory.conversations.get("room-1")
    assert conversation is not None and conversation.title == "Example"


async def test_changing_the_name_keeps_the_mode() -> None:
    """A rename must not silently return a group to the default mode."""
    directory = _directory()
    space_id = await _bind(directory, title="Example", bot_mode=BotMode.SILENT)
    await _bind(directory, space_id=space_id, title="Renamed")
    binding = await directory.resolve("custom-chat", "room-1")
    assert binding is not None
    assert binding.title == "Renamed"
    assert binding.bot_mode is BotMode.SILENT


async def test_a_new_binding_is_active() -> None:
    """A group nobody configured behaves like production: it answers."""
    directory = _directory()
    await _bind(directory, title="Example")
    binding = await directory.resolve("custom-chat", "room-1")
    assert binding is not None and binding.bot_mode is BotMode.ACTIVE


async def test_membership_requires_a_served_binding() -> None:
    """Only groups the operator still serves authorize a direct message."""
    directory = _directory()
    served = await _bind(directory, title="Served")
    other = MembershipDirectory(
        memberships=InMemorySpaceMembershipRepository(),
        bindings=directory.bindings,
        clock=directory.clock,
    )
    await other.observe("telegram:1", served)
    await other.observe("telegram:1", "sp_never_bound")
    assert await other.served_spaces("telegram:1", "custom-chat") == [served]


async def test_served_spaces_are_ordered_by_name() -> None:
    """Rendering is stable: global first, then groups by name."""
    directory = _directory()
    zebra = await _bind(directory, room="room-z", title="Zebra")
    alpha = await _bind(directory, room="room-a", title="Alpha")
    memberships = MembershipDirectory(
        memberships=InMemorySpaceMembershipRepository(),
        bindings=directory.bindings,
        clock=directory.clock,
    )
    await memberships.observe("telegram:1", zebra)
    await memberships.observe("telegram:1", alpha)
    assert await memberships.served_spaces("telegram:1", "custom-chat") == [
        alpha,
        zebra,
    ]
    assert await memberships.served_spaces("telegram:2", "custom-chat") == []


async def test_a_principal_who_left_loses_the_space() -> None:
    """Leaving a group withdraws what that group authorized."""
    directory = _directory()
    space_id = await _bind(directory, title="Example")
    memberships = MembershipDirectory(
        memberships=InMemorySpaceMembershipRepository(),
        bindings=directory.bindings,
        clock=directory.clock,
    )
    await memberships.observe("telegram:1", space_id)
    assert await memberships.served_spaces("telegram:1", "custom-chat") == [space_id]
    await memberships.mark_left("telegram:1", space_id)
    assert await memberships.served_spaces("telegram:1", "custom-chat") == []
    await memberships.observe("telegram:1", space_id)
    assert await memberships.served_spaces("telegram:1", "custom-chat") == [space_id]


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
