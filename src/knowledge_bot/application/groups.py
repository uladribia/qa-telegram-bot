# SPDX-License-Identifier: MIT
"""Manage logical spaces and channel bindings without channel-specific rules."""

from dataclasses import dataclass, replace

from knowledge_bot.domain.entities import ChannelBinding, Conversation, Source, Space
from knowledge_bot.domain.scope import new_space_id
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import (
    ChannelBindingRepository,
    ConversationRepository,
    SourceRepository,
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
    ) -> str:
        """Bind an external conversation to a logical space.

        Channel adapters supply all external values and source provenance. This
        service owns only the channel-independent space/binding relationship.
        """
        if not channel.strip() or not external_conversation_id.strip():
            message = "channel and external conversation id are required"
            raise ValueError(message)
        now = self.clock.now()
        resolved_space_id = space_id or new_space_id()
        if await self.spaces.get(resolved_space_id) is None:
            await self.spaces.add(
                Space(id=resolved_space_id, created_at=now, title=title)
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
                    title=title,
                )
            )
        elif (
            conversation.space_id != resolved_space_id
            or conversation.external_id != external_conversation_id
            or conversation.title != title
        ):
            await self.conversations.save(
                replace(
                    conversation,
                    space_id=resolved_space_id,
                    external_id=external_conversation_id,
                    title=title,
                )
            )
        existing = await self.bindings.get(channel, external_conversation_id)
        binding = ChannelBinding(
            channel=channel,
            external_conversation_id=external_conversation_id,
            conversation_id=conversation_id,
            space_id=resolved_space_id,
            created_at=existing.created_at if existing is not None else now,
            title=title,
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
