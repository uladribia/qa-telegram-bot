# SPDX-License-Identifier: MIT
"""Contract tests for the in-memory repository fakes.

These double as the behavioural contract every repository implementation (D1
included) must satisfy, and they run entirely in process.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from knowledge_bot.domain.entities import BotAnswer, Message, QAItem, QAVersion
from knowledge_bot.domain.enums import AnswerMode, ContentType, QAOrigin, QAStatus
from knowledge_bot.ports.repositories import (
    BotAnswerRepository,
    MessageRepository,
    QAItemRepository,
    QAVersionRepository,
    SourceRepository,
)
from tests.fakes.repositories import (
    InMemoryBotAnswerRepository,
    InMemoryMessageRepository,
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
    InMemorySourceRepository,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _message(message_id: str, external_id: str | None) -> Message:
    return Message(
        id=message_id,
        source_id="s1",
        conversation_id="c1",
        content_type=ContentType.TEXT,
        sent_at=NOW,
        created_at=NOW,
        external_id=external_id,
    )


def test_in_memory_repositories_satisfy_their_ports() -> None:
    """Fakes implement the ports they stand in for."""
    assert isinstance(InMemorySourceRepository(), SourceRepository)
    assert isinstance(InMemoryMessageRepository(), MessageRepository)
    assert isinstance(InMemoryQAItemRepository(), QAItemRepository)
    assert isinstance(InMemoryQAVersionRepository(), QAVersionRepository)
    assert isinstance(InMemoryBotAnswerRepository(), BotAnswerRepository)


def test_message_add_is_idempotent_by_external_id() -> None:
    """Re-processing the same external message does not duplicate state."""
    repository = InMemoryMessageRepository()
    assert repository.add(_message("m1", "ext-1")) is True
    assert repository.add(_message("m2", "ext-1")) is False
    assert repository.get("m2") is None
    stored = repository.get_by_external_id("s1", "ext-1")
    assert stored is not None
    assert stored.id == "m1"


def test_messages_without_external_id_are_always_added() -> None:
    """Messages with no external id have no idempotency key to collide on."""
    repository = InMemoryMessageRepository()
    assert repository.add(_message("m1", None)) is True
    assert repository.add(_message("m2", None)) is True


def test_qa_versioning_supersedes_without_deleting_history() -> None:
    """A new version becomes current while the old one stays queryable."""
    items = InMemoryQAItemRepository()
    versions = InMemoryQAVersionRepository()
    item = QAItem(
        id="q1",
        canonical_key="equipment",
        canonical_question="How do I order equipment?",
        status=QAStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    items.add(item)

    first = QAVersion(
        id="v1",
        qa_id="q1",
        answer="a1",
        authority=60,
        origin=QAOrigin.AUTO_GENERATED,
        created_at=NOW,
    )
    versions.add(first)
    items.save(replace(item, current_version_id="v1"))

    second = QAVersion(
        id="v2",
        qa_id="q1",
        answer="a2",
        authority=100,
        origin=QAOrigin.ADMIN_APPROVED,
        created_at=NOW,
        supersedes_version_id="v1",
    )
    versions.add(second)
    current = items.get("q1")
    assert current is not None
    items.save(replace(current, current_version_id="v2"))

    updated = items.get("q1")
    assert updated is not None
    assert updated.current_version_id == "v2"
    assert {version.id for version in versions.list_for_qa("q1")} == {"v1", "v2"}
    superseded = versions.get("v2")
    assert superseded is not None
    assert superseded.supersedes_version_id == "v1"


def test_bot_answers_can_be_listed_in_a_window() -> None:
    """Answer listing is half-open on the end of the window."""
    repository = InMemoryBotAnswerRepository()
    repository.add(_bot_answer("a1", minutes=0))
    repository.add(_bot_answer("a2", minutes=60))
    repository.add(_bot_answer("a3", minutes=120))
    window = repository.list_between(NOW, NOW + timedelta(minutes=90))
    assert [answer.id for answer in window] == ["a1", "a2"]


def _bot_answer(answer_id: str, *, minutes: int) -> BotAnswer:
    return BotAnswer(
        id=answer_id,
        conversation_id="c1",
        question="com?",
        answer="aixi",
        answer_mode=AnswerMode.DIRECT_QA,
        created_at=NOW + timedelta(minutes=minutes),
    )
