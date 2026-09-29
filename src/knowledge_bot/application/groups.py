# SPDX-License-Identifier: MIT
"""Manage logical spaces, channel bindings and observed memberships."""

from dataclasses import dataclass, replace

from knowledge_bot.domain.entities import ChannelBinding, Conversation, Source, Space
from knowledge_bot.domain.enums import BotMode
from knowledge_bot.domain.scope import new_space_id
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import (
    ChannelBindingRepository,
    ConversationRepository,
    SourceRepository,
    SpaceMembershipRepository,
    SpaceRepository,
)


@dataclass(frozen=True, slots=True)
class SpaceDirectory:
    """Create spaces and bind external conversations using opaque identifiers."""

    sources: SourceRepository
    conversations: ConversationRepository
    spaces: SpaceRepository
    bindings: ChannelBindingRepository
    clock: Clock

    async def bind(
        self,
        *,
        channel: str,
        external_conversation_id: str,
        conversation_id: str,
        space_id: str | None,
        title: str | None,
        source_id: str,
        source_kind: str,
        source_authority: int,
        bot_mode: BotMode | None = None,
    ) -> str:
        """Bind an external conversation to a logical space.

        Channel adapters supply all external values and source provenance. This
        service owns only the channel-independent space/binding relationship.

        Re-registering a conversation changes only what was supplied: an omitted
        ``title`` keeps the stored one and an omitted ``bot_mode`` keeps the
        stored mode. A caller that changes the mode therefore never renames a
        group by accident, and one that renames a group never changes how the
        bot behaves in it.

        Args:
            channel: The connector's channel name.
            external_conversation_id: The conversation id on that channel.
            conversation_id: The internal conversation id.
            space_id: The space to bind, or ``None`` to create a new one.
            title: The human name, or ``None`` to keep the stored one.
            source_id: The connector-declared source id.
            source_kind: The connector-declared source kind.
            source_authority: The connector-declared base authority.
            bot_mode: The bot's behaviour there, or ``None`` to keep it.

        Returns:
            The logical space id the conversation is now served under.
        """
        if not channel.strip() or not external_conversation_id.strip():
            message = "channel and external conversation id are required"
            raise ValueError(message)
        now = self.clock.now()
        resolved_space_id = space_id or new_space_id()
        existing = await self.bindings.get(channel, external_conversation_id)
        resolved_title = title if title is not None else _existing_title(existing)
        resolved_mode = bot_mode or (existing.bot_mode if existing else BotMode.ACTIVE)
        if await self.spaces.get(resolved_space_id) is None:
            await self.spaces.add(
                Space(id=resolved_space_id, created_at=now, title=resolved_title)
            )
        if await self.sources.get(source_id) is None:
            await self.sources.add(
                Source(
                    id=source_id,
                    source_type=source_kind,
                    authority=source_authority,
                    created_at=now,
                    title=source_kind,
                    is_mutable=True,
                )
            )
        conversation = await self.conversations.get(conversation_id)
        if conversation is None:
            await self.conversations.add(
                Conversation(
                    id=conversation_id,
                    source_id=source_id,
                    space_id=resolved_space_id,
                    created_at=now,
                    external_id=external_conversation_id,
                    title=resolved_title,
                )
            )
        elif (
            conversation.space_id != resolved_space_id
            or conversation.external_id != external_conversation_id
            or conversation.title != resolved_title
        ):
            await self.conversations.save(
                replace(
                    conversation,
                    space_id=resolved_space_id,
                    external_id=external_conversation_id,
                    title=resolved_title,
                )
            )
        binding = ChannelBinding(
            channel=channel,
            external_conversation_id=external_conversation_id,
            conversation_id=conversation_id,
            space_id=resolved_space_id,
            created_at=existing.created_at if existing is not None else now,
            title=resolved_title,
            bot_mode=resolved_mode,
        )
        if existing is None:
            await self.bindings.add(binding)
        elif existing != binding:
            await self.bindings.save(binding)
        return resolved_space_id

    async def resolve(
        self, channel: str, external_conversation_id: str
    ) -> ChannelBinding | None:
        """Resolve an external conversation to its logical space."""
        return await self.bindings.get(channel, external_conversation_id)


@dataclass(frozen=True, slots=True)
class MembershipDirectory:
    """Record who the bot has seen in a space, and which spaces they share with it.

    Membership is observation, not enumeration. The bot cannot list a group's
    members without admin rights, so it learns them one interaction at a time,
    and this service is the only place that writes that down. A person the bot
    has never seen is not a member: they are told how to introduce themselves
    instead.
    """

    memberships: SpaceMembershipRepository
    bindings: ChannelBindingRepository
    clock: Clock

    async def observe(self, principal_id: str, space_id: str) -> None:
        """Record that a principal was seen in a space.

        Args:
            principal_id: The channel-qualified principal identifier.
            space_id: The space the principal was seen in.
        """
        await self.memberships.observe(principal_id, space_id, self.clock.now())

    async def mark_left(self, principal_id: str, space_id: str) -> None:
        """Record that a principal left a space.

        Args:
            principal_id: The channel-qualified principal identifier.
            space_id: The space the principal left.
        """
        await self.memberships.mark_left(principal_id, space_id, self.clock.now())

    async def served_spaces(self, principal_id: str, channel: str) -> list[str]:
        """Return the spaces a principal shares with the bot, in display order.

        A membership alone is not enough: the space must still be served on
        that channel. A group the operator unbound stops authorizing a direct
        message the moment it is unbound, rather than granting access forever.

        Args:
            principal_id: The channel-qualified principal identifier.
            channel: The channel whose bindings count as served.

        Returns:
            Space ids ordered by title then id, so a rendered answer is stable.
        """
        active = set(await self.memberships.list_active_spaces(principal_id))
        if not active:
            return []
        served = [
            binding
            for binding in await self.bindings.list_by_channel(channel)
            if binding.space_id in active
        ]
        return [binding.space_id for binding in sorted(served, key=_display_order)]


def _existing_title(binding: ChannelBinding | None) -> str | None:
    """Return the title already stored for a binding, if any."""
    return binding.title if binding is not None else None


def _display_order(binding: ChannelBinding) -> tuple[str, str]:
    """Order bindings by name, falling back to the id when unnamed."""
    return (binding.title or binding.space_id, binding.space_id)
