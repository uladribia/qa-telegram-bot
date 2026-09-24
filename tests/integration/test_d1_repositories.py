# SPDX-License-Identifier: MIT
"""Integration tests for the D1 repositories against a SQLite-backed D1 fake."""

from datetime import UTC, datetime, timedelta

from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.contracts.messages import (
    AttachmentRef,
    NormalizedMessage,
    SourceDescriptor,
)
from knowledge_bot.domain.entities import (
    Attachment,
    BotAnswer,
    Conversation,
    Message,
    Source,
    Space,
)
from knowledge_bot.domain.enums import (
    AnswerMode,
    ContentType,
    ProcessingStatus,
)
from knowledge_bot.infrastructure.cloudflare.d1 import (
    D1AttachmentRepository,
    D1BotAnswerRepository,
    D1ChannelBindingRepository,
    D1ConversationRepository,
    D1MessageRepository,
    D1RecapStateRepository,
    D1ReviewSource,
    D1SearchProjectionRepository,
    D1SourceRepository,
    D1SpaceRepository,
)
from knowledge_bot.ports.repositories import (
    AttachmentRepository,
    BotAnswerRepository,
    MessageRepository,
    SourceRepository,
)
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.d1 import FakeD1Database

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)


def _repositories() -> tuple[
    FakeD1Database,
    D1SourceRepository,
    D1ConversationRepository,
    D1MessageRepository,
    D1AttachmentRepository,
    D1BotAnswerRepository,
    D1RecapStateRepository,
]:
    database = FakeD1Database()
    return (
        database,
        D1SourceRepository(database),
        D1ConversationRepository(database),
        D1MessageRepository(database),
        D1AttachmentRepository(database),
        D1BotAnswerRepository(database),
        D1RecapStateRepository(database),
    )


def _message(message_id: str, external_id: str | None) -> Message:
    return Message(
        id=message_id,
        source_id="telegram",
        conversation_id="-100",
        content_type=ContentType.TEXT,
        sent_at=NOW,
        created_at=NOW,
        external_id=external_id,
        text="hola",
    )


def test_d1_repositories_satisfy_their_ports() -> None:
    """The D1 adapters implement the repository ports."""
    _, sources, _, messages, attachments, answers, _ = _repositories()
    assert isinstance(sources, SourceRepository)
    assert isinstance(messages, MessageRepository)
    assert isinstance(attachments, AttachmentRepository)
    assert isinstance(answers, BotAnswerRepository)


async def test_source_round_trip_and_save() -> None:
    """Sources can be inserted, read, and updated."""
    _, sources, _, _, _, _, _ = _repositories()
    await sources.add(
        Source(
            id="telegram",
            source_type="telegram",
            authority=40,
            created_at=NOW,
            title="telegram",
        )
    )
    loaded = await sources.get("telegram")
    assert loaded is not None
    assert loaded.source_type == "telegram"
    await sources.save(
        Source(
            id="telegram",
            source_type="telegram",
            authority=95,
            created_at=NOW,
            title="renamed",
        )
    )
    updated = await sources.get("telegram")
    assert updated is not None
    assert updated.authority == 95
    assert updated.title == "renamed"


async def test_conversation_round_trip() -> None:
    """Conversations depend on their source."""
    database, sources, conversations, _, _, _, _ = _repositories()
    await sources.add(
        Source(id="telegram", source_type="telegram", authority=40, created_at=NOW)
    )
    space_id = "sp_" + "2" * 32
    await D1SpaceRepository(database).add(
        Space(id=space_id, title="Example", created_at=NOW)
    )
    await conversations.add(
        Conversation(
            id="-100",
            source_id="telegram",
            space_id=space_id,
            created_at=NOW,
            external_id="-100",
        )
    )
    loaded = await conversations.get("-100")
    assert loaded is not None
    assert loaded.source_id == "telegram"
    assert loaded.space_id == space_id


async def test_search_projection_manifest_round_trip() -> None:
    """The SQL manifest follows successful vector projection changes."""
    from knowledge_bot.ports.index import SearchProjectionRepository

    database = FakeD1Database()
    repository = D1SearchProjectionRepository(database)
    assert isinstance(repository, SearchProjectionRepository)
    record = VectorRecord(
        id="qa:q1",
        values=[1.0, 0.0],
        metadata={"kind": "qa", "object_id": "q1"},
    )
    await repository.record([record], NOW)
    assert await repository.list_vector_ids() == ["qa:q1"]
    await repository.delete(["qa:q1"])
    assert await repository.list_vector_ids() == []


async def test_space_and_channel_binding_round_trip() -> None:
    """D1 persists logical spaces independently from connector identities."""
    from knowledge_bot.domain.entities import ChannelBinding, Space

    database = FakeD1Database()
    spaces = D1SpaceRepository(database)
    bindings = D1ChannelBindingRepository(database)
    space_id = "sp_" + "1" * 32
    await spaces.add(Space(id=space_id, title="Example", created_at=NOW))
    await bindings.add(
        ChannelBinding(
            channel="custom-chat",
            external_conversation_id="room-1",
            conversation_id="conversation-1",
            space_id=space_id,
            title="Room",
            created_at=NOW,
        )
    )
    assert await spaces.get(space_id) == Space(
        id=space_id, title="Example", created_at=NOW
    )
    binding = await bindings.get("custom-chat", "room-1")
    assert binding is not None and binding.space_id == space_id


async def _seed_telegram(
    sources: D1SourceRepository,
    conversations: D1ConversationRepository,
) -> None:
    await sources.add(
        Source(id="telegram", source_type="telegram", authority=40, created_at=NOW)
    )
    await conversations.add(
        Conversation(
            id="-100", source_id="telegram", created_at=NOW, external_id="-100"
        )
    )


async def test_message_idempotency_is_enforced_by_sql() -> None:
    """The unique key plus the pre-check prevent duplicates."""
    _, sources, conversations, messages, _, _, _ = _repositories()
    await _seed_telegram(sources, conversations)
    assert await messages.add(_message("m1", "-100:10")) is True
    assert await messages.add(_message("m2", "-100:10")) is False
    assert await messages.get("m2") is None
    existing = await messages.get_by_external_id("telegram", "-100:10")
    assert existing is not None
    assert existing.id == "m1"


async def test_attachments_round_trip() -> None:
    """Attachment metadata is stored and listed."""
    _, sources, conversations, messages, attachments, _, _ = _repositories()
    await _seed_telegram(sources, conversations)
    await messages.add(_message("m1", "-100:10"))
    await attachments.add(
        Attachment(
            id="m1:0",
            message_id="m1",
            kind="image",
            processing_status=ProcessingStatus.UNPROCESSED,
            created_at=NOW,
            external_file_id="file-1",
            width=1280,
        )
    )
    stored = await attachments.list_for_message("m1")
    assert len(stored) == 1
    assert stored[0].external_file_id == "file-1"
    assert stored[0].width == 1280


async def test_bot_answers_window_query() -> None:
    """Answers are listed within a half-open window."""
    _, sources, conversations, _, _, answers, _ = _repositories()
    await _seed_telegram(sources, conversations)
    space_id = "sp_" + "3" * 32
    for index, answer_id in enumerate(("a1", "a2", "a3")):
        await answers.add(
            BotAnswer(
                id=answer_id,
                conversation_id="-100",
                space_id=space_id,
                question=f"q{index}",
                answer="a",
                answer_mode=AnswerMode.DIRECT_QA,
                created_at=NOW + timedelta(minutes=index * 60),
            )
        )
    window = await answers.list_between(NOW, NOW + timedelta(minutes=90))
    assert [answer.id for answer in window] == ["a1", "a2"]
    assert all(answer.space_id == space_id for answer in window)


async def test_recap_state_upsert() -> None:
    """Recap state is created once and overwritten afterwards."""
    _, _, _, _, _, _, recap_state = _repositories()
    assert await recap_state.get_last_sent_at("-100") is None
    await recap_state.set_last_sent_at("-100", NOW)
    assert await recap_state.get_last_sent_at("-100") == NOW
    later = NOW + timedelta(hours=25)
    await recap_state.set_last_sent_at("-100", later)
    assert await recap_state.get_last_sent_at("-100") == later


async def test_review_source_reads_current_versions_and_superseded_origin() -> None:
    """The review source joins the current version and the superseded origin."""
    database = FakeD1Database()
    connection = database.connection
    connection.execute(
        "INSERT INTO qa_items"
        " (id, canonical_key, canonical_question, status, current_version_id,"
        " created_at, updated_at, scope_key) VALUES"
        " ('q1','k1','Què?','active','v2','2026-01-01','2026-01-02','global')"
    )
    connection.execute(
        "INSERT INTO qa_versions"
        " (id, qa_id, answer, authority, origin, created_at) VALUES"
        " ('v1','q1','Antiga',90,'web_seed','2026-01-01')"
    )
    connection.execute(
        "INSERT INTO qa_versions"
        " (id, qa_id, answer, authority, origin, created_at,"
        " supersedes_version_id) VALUES"
        " ('v2','q1','Nova',90,'web_seed','2026-01-02','v1')"
    )
    connection.commit()
    items = await D1ReviewSource(database).list_current()
    assert len(items) == 1
    assert items[0].answer == "Nova"
    assert items[0].superseded_origin == "web_seed"
    assert items[0].status == "active"


async def test_ingest_use_case_against_d1_repository() -> None:
    """The whole ingest flow works with real SQL, not just the fakes."""
    database = FakeD1Database()
    ingestor = MessageIngestor(
        sources=D1SourceRepository(database),
        conversations=D1ConversationRepository(database),
        messages=D1MessageRepository(database),
        attachments=D1AttachmentRepository(database),
    )
    normalized = NormalizedMessage(
        id="tg:-100:10",
        source=SourceDescriptor(
            id="src:telegram:runtime", kind="telegram", authority=40
        ),
        conversation_id="-100",
        sender_is_admin=False,
        timestamp=NOW,
        content_type=ContentType.IMAGE,
        source_message_id="-100:10",
        sender_id="hash",
        text="mira",
        attachments=[AttachmentRef(kind="image", external_id="file-1", width=1080)],
    )
    assert (await ingestor.ingest(normalized)).created is True
    assert (await ingestor.ingest(normalized)).created is False
    stored = await ingestor.messages.get("tg:-100:10")
    assert stored is not None
    assert stored.text == "mira"
    attachments = await ingestor.attachments.list_for_message("tg:-100:10")
    assert len(attachments) == 1


async def test_reviewer_repositories_round_trip() -> None:
    """Reviewer nomination, replacement, removal, and events survive D1 SQL."""
    from dataclasses import replace

    from knowledge_bot.domain.entities import Reviewer, ReviewerEvent
    from knowledge_bot.domain.scope import GLOBAL_SCOPE
    from knowledge_bot.infrastructure.cloudflare.d1 import (
        D1ReportStateRepository,
        D1ReviewerEventRepository,
        D1ReviewerRepository,
    )

    database = FakeD1Database()
    reviewers = D1ReviewerRepository(database)
    assert await reviewers.get(GLOBAL_SCOPE) is None
    await reviewers.save(Reviewer(GLOBAL_SCOPE, "222", "Pepe", NOW, "1"))
    await reviewers.save(Reviewer(GLOBAL_SCOPE, "333", "Marta", NOW, "1"))
    stored = await reviewers.get(GLOBAL_SCOPE)
    assert stored is not None and stored.user_id == "333" and stored.name == "Marta"
    assert [r.user_id for r in await reviewers.all()] == ["333"]
    assert await reviewers.delete(GLOBAL_SCOPE) is True
    assert await reviewers.delete(GLOBAL_SCOPE) is False

    events = D1ReviewerEventRepository(database)
    event = await events.add(
        ReviewerEvent(
            feedback_id="fb:1",
            action="approved",
            created_at=NOW,
            reviewer_name="Pepe",
            group_label="Prebenjamins",
            question="Com?",
            approval_scope=GLOBAL_SCOPE,
        )
    )
    assert event.feedback_id == "fb:1"
    await events.add(replace(event, feedback_id="fb:2", action="rejected"))
    assert len(await events.list_unreported()) == 2
    await events.mark_reported(["fb:1"])
    pending = await events.list_unreported()
    assert [e.feedback_id for e in pending] == ["fb:2"]

    state = D1ReportStateRepository(database)
    assert await state.get_last_sent_at() is None
    await state.set_last_sent_at(NOW)
    assert await state.get_last_sent_at() == NOW
