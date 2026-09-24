# SPDX-License-Identifier: MIT
"""D1-backed repository implementations.

The D1 binding is asynchronous: ``db.prepare(sql).bind(...)`` then
``await stmt.first()`` or ``await stmt.run()``. Results are converted to Python
defensively because bindings can return Pyodide proxies.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, cast

from knowledge_bot.domain.entities import (
    Attachment,
    BotAnswer,
    ChannelBinding,
    Conversation,
    Feedback,
    Message,
    QAEvidence,
    QAItem,
    QAVersion,
    Reviewer,
    ReviewerEvent,
    Source,
    Space,
)
from knowledge_bot.domain.enums import (
    AnswerMode,
    ClassificationStatus,
    ContentType,
    FeedbackStatus,
    IndexStatus,
    ProcessingStatus,
    QAStatus,
)
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.index import IndexableMessage, IndexableQA
from knowledge_bot.ports.review import ReviewItem
from knowledge_bot.ports.transactions import ApproveCorrectionCommand
from knowledge_bot.ports.vector_store import VectorRecord


class D1Result(Protocol):
    """The subset of a D1 result the adapter reads."""

    results: list[dict[str, object]]


class D1Statement(Protocol):
    """A prepared D1 statement."""

    def bind(self, *params: object) -> "D1Statement":
        """Bind positional parameters."""
        ...

    async def first(self) -> dict[str, object] | None:
        """Return the first row, if any."""
        ...

    async def run(self) -> D1Result:
        """Execute the statement."""
        ...


class D1Database(Protocol):
    """The subset of the D1 binding the adapter uses."""

    def prepare(self, sql: str) -> D1Statement:
        """Prepare a statement."""
        ...

    async def batch(self, statements: Sequence[D1Statement]) -> object:
        """Execute statements in one transaction."""
        ...


def _to_python(value: object) -> object:
    converter = getattr(value, "to_py", None)
    return converter() if callable(converter) else value


def _rows(result: D1Result) -> list[dict[str, object]]:
    raw = cast("list[dict[str, object]]", _to_python(result.results))
    return [dict(row) for row in raw]


def _row(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return cast("dict[str, object]", _to_python(value))


def _iso(value: datetime) -> str:
    return value.isoformat()


def _dt(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: object) -> int | None:
    return None if value is None else int(cast(int, value))


def _opt_float(value: object) -> float | None:
    """Return a float score, or ``None`` when the column is missing."""
    if value is None:
        return None
    try:
        return float(cast(float, value))
    except (TypeError, ValueError):
        return None


def _opt_dt(value: object) -> datetime | None:
    return None if value is None else _dt(value)


def _date_part(value: object) -> str | None:
    """Return a DD/MM/YYYY date for a stored timestamp, if any."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).strftime("%d/%m/%Y")
    except ValueError:
        return str(value)[:10]


def _datetime_part(value: object) -> str | None:
    """Return a DD/MM/YYYY HH:MM stamp for a stored timestamp, if any."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(value)


def _exact_url(base: object, anchor: object) -> str | None:
    """Compose the anchor-specific URL for a web Q&A, when possible.

    The anchor is only appended when the stored URL is the web snapshot itself.
    A correction stores no URL, and its anchor is an opaque canonical key.
    """
    base_text = _opt_str(base)
    anchor_text = _opt_str(anchor)
    if not base_text:
        return None
    if anchor_text and base_text.endswith("/"):
        return f"{base_text}#{anchor_text}"
    return base_text


def _sender_label(sender_name: object, sender_hash: object) -> str | None:
    """Return the display name for a citation, falling back to a pseudonym."""
    if sender_name:
        return str(sender_name)
    if sender_hash:
        return f"\u00b7{str(sender_hash)[:6]}"
    return None


class D1SpaceRepository:
    """D1 implementation of ``SpaceRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, space: Space) -> None:
        """Persist a new logical space."""
        await (
            self._db.prepare(
                "INSERT INTO spaces (id, title, created_at) VALUES (?, ?, ?)"
            )
            .bind(space.id, space.title, _iso(space.created_at))
            .run()
        )

    async def get(self, space_id: str) -> Space | None:
        """Return a logical space by id."""
        row = _row(
            await self._db.prepare("SELECT * FROM spaces WHERE id = ?")
            .bind(space_id)
            .first()
        )
        if row is None:
            return None
        return Space(
            id=str(row["id"]),
            title=_opt_str(row["title"]),
            created_at=_dt(row["created_at"]),
        )


class D1ChannelBindingRepository:
    """D1 implementation of ``ChannelBindingRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def get(
        self, channel: str, external_conversation_id: str
    ) -> ChannelBinding | None:
        """Return a channel binding by external conversation id."""
        row = _row(
            await self._db.prepare(
                "SELECT * FROM channel_bindings"
                " WHERE channel = ? AND external_conversation_id = ?"
            )
            .bind(channel, external_conversation_id)
            .first()
        )
        if row is None:
            return None
        return _channel_binding(row)

    async def add(self, binding: ChannelBinding) -> None:
        """Persist a new channel binding."""
        await (
            self._db.prepare(
                "INSERT INTO channel_bindings"
                " (channel, external_conversation_id, conversation_id, space_id,"
                " title, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            )
            .bind(
                binding.channel,
                binding.external_conversation_id,
                binding.conversation_id,
                binding.space_id,
                binding.title,
                _iso(binding.created_at),
            )
            .run()
        )

    async def save(self, binding: ChannelBinding) -> None:
        """Persist changes to an existing channel binding."""
        await (
            self._db.prepare(
                "UPDATE channel_bindings SET conversation_id = ?, space_id = ?,"
                " title = ? WHERE channel = ? AND external_conversation_id = ?"
            )
            .bind(
                binding.conversation_id,
                binding.space_id,
                binding.title,
                binding.channel,
                binding.external_conversation_id,
            )
            .run()
        )


def _channel_binding(row: dict[str, object]) -> ChannelBinding:
    return ChannelBinding(
        channel=str(row["channel"]),
        external_conversation_id=str(row["external_conversation_id"]),
        conversation_id=str(row["conversation_id"]),
        space_id=str(row["space_id"]),
        title=_opt_str(row["title"]),
        created_at=_dt(row["created_at"]),
    )


class D1SourceRepository:
    """D1 implementation of ``SourceRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, source: Source) -> None:
        """Persist a new source."""
        await (
            self._db.prepare(
                "INSERT INTO sources"
                " (id, source_type, external_ref, title, canonical_url,"
                " authority, is_mutable, created_at, scope_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                source.id,
                source.source_type,
                source.external_ref,
                source.title,
                source.canonical_url,
                source.authority,
                int(source.is_mutable),
                _iso(source.created_at),
                source.scope_key,
            )
            .run()
        )

    async def get(self, source_id: str) -> Source | None:
        """Return a source by id, if present."""
        row = _row(
            await self._db.prepare("SELECT * FROM sources WHERE id = ?")
            .bind(source_id)
            .first()
        )
        if row is None:
            return None
        return Source(
            id=str(row["id"]),
            source_type=str(row["source_type"]),
            authority=int(cast(int, row["authority"])),
            created_at=_dt(row["created_at"]),
            external_ref=_opt_str(row["external_ref"]),
            title=_opt_str(row["title"]),
            canonical_url=_opt_str(row["canonical_url"]),
            is_mutable=bool(row["is_mutable"]),
            scope_key=str(row["scope_key"]),
        )

    async def save(self, source: Source) -> None:
        """Persist changes to an existing source."""
        await (
            self._db.prepare(
                "UPDATE sources SET source_type = ?, external_ref = ?, title = ?,"
                " canonical_url = ?, authority = ?, is_mutable = ?, scope_key = ?"
                " WHERE id = ?"
            )
            .bind(
                source.source_type,
                source.external_ref,
                source.title,
                source.canonical_url,
                source.authority,
                int(source.is_mutable),
                source.scope_key,
                source.id,
            )
            .run()
        )


class D1ConversationRepository:
    """D1 implementation of ``ConversationRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, conversation: Conversation) -> None:
        """Persist a new conversation."""
        await (
            self._db.prepare(
                "INSERT INTO conversations"
                " (id, source_id, space_id, external_id, title, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
            )
            .bind(
                conversation.id,
                conversation.source_id,
                conversation.space_id,
                conversation.external_id,
                conversation.title,
                _iso(conversation.created_at),
            )
            .run()
        )

    async def get(self, conversation_id: str) -> Conversation | None:
        """Return a conversation by id, if present."""
        row = _row(
            await self._db.prepare("SELECT * FROM conversations WHERE id = ?")
            .bind(conversation_id)
            .first()
        )
        if row is None:
            return None
        return Conversation(
            id=str(row["id"]),
            source_id=str(row["source_id"]),
            space_id=_opt_str(row.get("space_id")),
            created_at=_dt(row["created_at"]),
            external_id=_opt_str(row["external_id"]),
            title=_opt_str(row["title"]),
        )

    async def save(self, conversation: Conversation) -> None:
        """Persist changes to an existing conversation."""
        await (
            self._db.prepare(
                "UPDATE conversations SET source_id = ?, space_id = ?,"
                " external_id = ?, title = ? WHERE id = ?"
            )
            .bind(
                conversation.source_id,
                conversation.space_id,
                conversation.external_id,
                conversation.title,
                conversation.id,
            )
            .run()
        )


class D1MessageRepository:
    """D1 implementation of ``MessageRepository`` with idempotent insertion."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, message: Message) -> bool:
        """Persist a message; return ``False`` when it already exists."""
        if await self.get(message.id) is not None:
            return False
        if (
            message.external_id is not None
            and (await self.get_by_external_id(message.source_id, message.external_id))
            is not None
        ):
            return False
        await (
            self._db.prepare(
                "INSERT INTO messages"
                " (id, source_id, conversation_id, external_id, sender_hash,"
                " sender_name, sender_is_admin, sender_authority, sent_at, text,"
                " content_type, reply_to_message_id, created_at, intent_label,"
                " intent_score, context_question, classification_status,"
                " intent_scores_json,"
                " index_status, indexed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                message.id,
                message.source_id,
                message.conversation_id,
                message.external_id,
                message.sender_hash,
                message.sender_name,
                int(message.sender_is_admin),
                message.sender_authority,
                _iso(message.sent_at),
                message.text,
                message.content_type.value,
                message.reply_to_message_id,
                _iso(message.created_at),
                message.intent_label,
                message.intent_score,
                message.context_question,
                message.classification_status.value,
                message.intent_scores_json,
                message.index_status.value,
                _iso(message.indexed_at) if message.indexed_at is not None else None,
            )
            .run()
        )
        return True

    async def get(self, message_id: str) -> Message | None:
        """Return a message by id, if present."""
        row = _row(
            await self._db.prepare("SELECT * FROM messages WHERE id = ?")
            .bind(message_id)
            .first()
        )
        return _message(row) if row is not None else None

    async def save(self, message: Message) -> None:
        """Persist classification and indexing state changes."""
        await (
            self._db.prepare(
                "UPDATE messages SET intent_label = ?, intent_score = ?,"
                " context_question = ?, classification_status = ?,"
                " intent_scores_json = ?, index_status = ?, indexed_at = ?"
                " WHERE id = ?"
            )
            .bind(
                message.intent_label,
                message.intent_score,
                message.context_question,
                message.classification_status.value,
                message.intent_scores_json,
                message.index_status.value,
                _iso(message.indexed_at) if message.indexed_at is not None else None,
                message.id,
            )
            .run()
        )

    async def get_by_external_id(
        self, source_id: str, external_id: str
    ) -> Message | None:
        """Return a message by its idempotency key, if present."""
        row = _row(
            await self._db.prepare(
                "SELECT * FROM messages WHERE source_id = ? AND external_id = ?"
            )
            .bind(source_id, external_id)
            .first()
        )
        return _message(row) if row is not None else None

    async def list_by_classification_status(
        self, status: str, limit: int
    ) -> list[Message]:
        """Return a bounded batch with the requested classification state."""
        result = (
            await self._db.prepare(
                "SELECT * FROM messages WHERE classification_status = ?"
                " ORDER BY created_at LIMIT ?"
            )
            .bind(status, limit)
            .run()
        )
        return [_message(row) for row in _rows(result)]

    async def listener_stats_between(
        self, start: datetime, end: datetime
    ) -> tuple[int, int]:
        """Return ``(ingested, paired)`` listener counts in ``[start, end)``."""
        ingested = _row(
            await self._db.prepare(
                "SELECT COUNT(*) AS n FROM messages"
                " WHERE intent_label IS NOT NULL"
                " AND created_at >= ? AND created_at < ?"
            )
            .bind(_iso(start), _iso(end))
            .first()
        )
        paired = _row(
            await self._db.prepare(
                "SELECT COUNT(*) AS n FROM messages"
                " WHERE context_question IS NOT NULL"
                " AND created_at >= ? AND created_at < ?"
            )
            .bind(_iso(start), _iso(end))
            .first()
        )
        return (_count(ingested), _count(paired))


class D1AttachmentRepository:
    """D1 implementation of ``AttachmentRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, attachment: Attachment) -> None:
        """Persist attachment metadata."""
        await (
            self._db.prepare(
                "INSERT INTO attachments"
                " (id, message_id, kind, external_file_id, external_unique_id,"
                " file_name,"
                " mime_type, width, height, size_bytes, processing_status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                attachment.id,
                attachment.message_id,
                attachment.kind,
                attachment.external_file_id,
                attachment.external_unique_id,
                attachment.file_name,
                attachment.mime_type,
                attachment.width,
                attachment.height,
                attachment.size_bytes,
                attachment.processing_status.value,
                _iso(attachment.created_at),
            )
            .run()
        )

    async def list_for_message(self, message_id: str) -> list[Attachment]:
        """Return the attachments of a message."""
        result = (
            await self._db.prepare("SELECT * FROM attachments WHERE message_id = ?")
            .bind(message_id)
            .run()
        )
        return [_attachment(row) for row in _rows(result)]


class D1BotAnswerRepository:
    """D1 implementation of ``BotAnswerRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, answer: BotAnswer) -> None:
        """Persist a bot answer."""
        await (
            self._db.prepare(
                "INSERT INTO bot_answers"
                " (id, conversation_id, space_id, user_message_id,"
                " telegram_bot_message_id, question, answer, answer_mode, confidence,"
                " qa_version_id, sources_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                answer.id,
                answer.conversation_id,
                answer.space_id,
                answer.user_message_id,
                answer.telegram_bot_message_id,
                answer.question,
                answer.answer,
                answer.answer_mode.value,
                answer.confidence,
                answer.qa_version_id,
                answer.sources_json,
                _iso(answer.created_at),
            )
            .run()
        )

    async def get(self, answer_id: str) -> BotAnswer | None:
        """Return a bot answer by id, if present."""
        row = _row(
            await self._db.prepare("SELECT * FROM bot_answers WHERE id = ?")
            .bind(answer_id)
            .first()
        )
        return _bot_answer(row) if row is not None else None

    async def list_between(self, start: datetime, end: datetime) -> list[BotAnswer]:
        """Return the answers created in ``[start, end)``."""
        result = (
            await self._db.prepare(
                "SELECT * FROM bot_answers WHERE created_at >= ?"
                " AND created_at < ? ORDER BY created_at"
            )
            .bind(_iso(start), _iso(end))
            .run()
        )
        return [_bot_answer(row) for row in _rows(result)]


class D1RecapStateRepository:
    """D1 implementation of ``RecapStateRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def get_last_sent_at(self, conversation_id: str) -> datetime | None:
        """Return when the last recap was sent, if ever."""
        row = _row(
            await self._db.prepare(
                "SELECT last_sent_at FROM recap_state WHERE conversation_id = ?"
            )
            .bind(conversation_id)
            .first()
        )
        return _dt(row["last_sent_at"]) if row is not None else None

    async def set_last_sent_at(self, conversation_id: str, sent_at: datetime) -> None:
        """Record when a recap was sent."""
        await (
            self._db.prepare(
                "INSERT INTO recap_state (conversation_id, last_sent_at) VALUES (?, ?)"
                " ON CONFLICT(conversation_id) DO UPDATE SET"
                " last_sent_at = excluded.last_sent_at"
            )
            .bind(conversation_id, _iso(sent_at))
            .run()
        )


def _count(value: dict[str, object] | None) -> int:
    """Return the ``n`` of a ``COUNT(*)`` row, or ``0`` when missing."""
    if value is None:
        return 0
    raw = value.get("n", 0)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _message(row: dict[str, object]) -> Message:
    return Message(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        conversation_id=str(row["conversation_id"]),
        content_type=ContentType(str(row["content_type"])),
        sent_at=_dt(row["sent_at"]),
        created_at=_dt(row["created_at"]),
        sender_is_admin=bool(row["sender_is_admin"]),
        sender_authority=_opt_int(row.get("sender_authority")),
        external_id=_opt_str(row["external_id"]),
        sender_hash=_opt_str(row["sender_hash"]),
        sender_name=_opt_str(row["sender_name"]),
        text=_opt_str(row["text"]),
        reply_to_message_id=_opt_str(row["reply_to_message_id"]),
        intent_label=_opt_str(row.get("intent_label")),
        intent_score=_opt_float(row.get("intent_score")),
        context_question=_opt_str(row.get("context_question")),
        classification_status=ClassificationStatus(
            str(
                row.get("classification_status")
                or ClassificationStatus.NOT_CLASSIFIED.value
            )
        ),
        intent_scores_json=_opt_str(row.get("intent_scores_json")),
        index_status=IndexStatus(
            str(row.get("index_status") or IndexStatus.NOT_INDEXED.value)
        ),
        indexed_at=_opt_dt(row.get("indexed_at")),
    )


def _attachment(row: dict[str, object]) -> Attachment:
    return Attachment(
        id=str(row["id"]),
        message_id=str(row["message_id"]),
        kind=str(row["kind"]),
        processing_status=ProcessingStatus(str(row["processing_status"])),
        created_at=_dt(row["created_at"]),
        external_file_id=_opt_str(row["external_file_id"]),
        external_unique_id=_opt_str(row["external_unique_id"]),
        file_name=_opt_str(row["file_name"]),
        mime_type=_opt_str(row["mime_type"]),
        width=_opt_int(row["width"]),
        height=_opt_int(row["height"]),
        size_bytes=_opt_int(row["size_bytes"]),
    )


def _bot_answer(row: dict[str, object]) -> BotAnswer:
    return BotAnswer(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        space_id=_opt_str(row.get("space_id")),
        question=str(row["question"]),
        answer=str(row["answer"]),
        answer_mode=AnswerMode(str(row["answer_mode"])),
        created_at=_dt(row["created_at"]),
        user_message_id=_opt_str(row["user_message_id"]),
        telegram_bot_message_id=_opt_str(row["telegram_bot_message_id"]),
        confidence=float(cast(float, row["confidence"]))
        if row["confidence"] is not None
        else None,
        qa_version_id=_opt_str(row["qa_version_id"]),
        sources_json=str(row["sources_json"]),
    )


def _message_authority(row: dict[str, object]) -> int:
    """Return the connector-declared source authority for an indexed message."""
    return int(cast(int, row["source_authority"]))


class D1SearchProjectionRepository:
    """D1 implementation of the derived vector projection manifest."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def list_vector_ids(self) -> list[str]:
        """Return every projected vector id."""
        result = await self._db.prepare(
            "SELECT vector_id FROM search_projection ORDER BY vector_id"
        ).run()
        return [str(row["vector_id"]) for row in _rows(result)]

    async def record(self, records: list[VectorRecord], updated_at: datetime) -> None:
        """Record successfully upserted vectors."""
        for record in records:
            metadata = record.metadata
            await (
                self._db.prepare(
                    "INSERT INTO search_projection"
                    " (vector_id, kind, object_id, updated_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(vector_id) DO UPDATE SET kind = excluded.kind,"
                    " object_id = excluded.object_id, updated_at = excluded.updated_at"
                )
                .bind(
                    record.id,
                    str(metadata.get("kind", "unknown")),
                    str(metadata.get("object_id", record.id)),
                    _iso(updated_at),
                )
                .run()
            )

    async def delete(self, vector_ids: list[str]) -> None:
        """Remove deleted vector ids from the manifest."""
        for vector_id in vector_ids:
            await (
                self._db.prepare("DELETE FROM search_projection WHERE vector_id = ?")
                .bind(vector_id)
                .run()
            )

    async def clear(self) -> None:
        """Clear the projection manifest."""
        await self._db.prepare("DELETE FROM search_projection").run()


class D1SearchIndexSource:
    """D1 implementation of ``SearchIndexSource`` for reindexing."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def get_qa(self, version_id: str) -> IndexableQA | None:
        """Return one active Q&A version to index, by version id."""
        row = _row(
            await self._db.prepare(
                "SELECT qi.id AS item_id, qv.id AS version_id,"
                " qi.canonical_question AS question, qv.answer AS answer,"
                " qv.authority AS authority, qi.canonical_key AS canonical_key,"
                " qv.source_url AS source_url, qv.source_anchor AS source_anchor,"
                " qv.author AS author, qv.created_at AS created_at,"
                " qi.scope_key AS scope_key"
                " FROM qa_versions qv"
                " JOIN qa_items qi ON qi.id = qv.qa_id"
                " WHERE qi.status = 'active' AND qv.id = ?"
            )
            .bind(version_id)
            .first()
        )
        if row is None:
            return None
        return IndexableQA(
            qa_item_id=str(row["item_id"]),
            version_id=str(row["version_id"]),
            question=str(row["question"]),
            answer=str(row["answer"]),
            authority=int(cast(int, row["authority"])),
            canonical_key=str(row["canonical_key"]),
            source_anchor=_opt_str(row["source_anchor"]),
            url=_exact_url(row["source_url"], row["source_anchor"]),
            date=_date_part(row["created_at"]),
            author=_opt_str(row["author"]),
            scope_key=str(row["scope_key"]),
        )

    async def list_qa(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableQA]:
        """Return the active Q&A versions to index, in id-order batches.

        Args:
            after: Only versions with id greater than this (cursor).
            limit: Maximum batch size.

        Returns:
            The next batch of versions.
        """
        query = (
            "SELECT qi.id AS item_id, qv.id AS version_id,"
            " qi.canonical_question AS question, qv.answer AS answer,"
            " qv.authority AS authority, qi.canonical_key AS canonical_key,"
            " qv.source_url AS source_url, qv.source_anchor AS source_anchor,"
            " qv.author AS author, qv.created_at AS created_at,"
            " qi.scope_key AS scope_key"
            " FROM qa_versions qv"
            " JOIN qa_items qi ON qi.id = qv.qa_id"
            " WHERE qi.status = 'active' AND qi.current_version_id = qv.id"
        )
        params: list[object] = []
        if after is not None:
            query += " AND qv.id > ?"
            params.append(after)
        query += " ORDER BY qv.id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        result = await self._db.prepare(query).bind(*params).run()
        return [
            IndexableQA(
                qa_item_id=str(row["item_id"]),
                version_id=str(row["version_id"]),
                question=str(row["question"]),
                answer=str(row["answer"]),
                authority=int(cast(int, row["authority"])),
                canonical_key=str(row["canonical_key"]),
                source_anchor=_opt_str(row["source_anchor"]),
                url=_exact_url(row["source_url"], row["source_anchor"]),
                date=_date_part(row["created_at"]),
                author=_opt_str(row["author"]),
                scope_key=str(row["scope_key"]),
            )
            for row in _rows(result)
        ]

    async def list_messages(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableMessage]:
        """Return the messages with text to index, in id-order batches."""
        query = (
            "SELECT m.id, m.source_id, m.conversation_id, m.text,"
            " m.sender_hash, m.sender_name, m.sent_at, m.context_question,"
            " c.space_id, s.source_type AS source_kind, s.authority AS source_authority"
            " FROM messages m JOIN sources s ON s.id = m.source_id"
            " JOIN conversations c ON c.id = m.conversation_id"
            " WHERE m.text IS NOT NULL AND m.text != '' AND m.index_status = 'indexed'"
        )
        params: list[object] = []
        if after is not None:
            query += " AND m.id > ?"
            params.append(after)
        query += " ORDER BY m.id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        result = await self._db.prepare(query).bind(*params).run()
        return [
            IndexableMessage(
                message_id=str(row["id"]),
                text=str(row["text"]),
                source_kind=str(row["source_kind"]),
                authority=_message_authority(row),
                conversation_id=str(row["conversation_id"]),
                scope_key=scope_for_space(str(row["space_id"]))
                if row.get("space_id")
                else GLOBAL_SCOPE,
                author=_sender_label(row["sender_name"], row["sender_hash"]),
                date=_datetime_part(row["sent_at"]),
                question=_opt_str(row.get("context_question")),
            )
            for row in _rows(result)
        ]


class D1QAItemRepository:
    """D1 implementation of ``QAItemRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, item: QAItem) -> None:
        """Persist a new Q&A item."""
        await (
            self._db.prepare(
                "INSERT INTO qa_items"
                " (id, canonical_key, canonical_question, status, current_version_id,"
                " created_at, updated_at, scope_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                item.id,
                item.canonical_key,
                item.canonical_question,
                item.status.value,
                item.current_version_id,
                _iso(item.created_at),
                _iso(item.updated_at),
                item.scope_key,
            )
            .run()
        )

    async def get(self, qa_id: str) -> QAItem | None:
        """Return a Q&A item by id, if present."""
        row = _row(
            await self._db.prepare("SELECT * FROM qa_items WHERE id = ?")
            .bind(qa_id)
            .first()
        )
        return _qa_item(row) if row is not None else None

    async def get_by_canonical_key(
        self,
        canonical_key: str,
        scope_key: str = "global",
    ) -> QAItem | None:
        """Return a Q&A item by canonical key within a scope, if present."""
        row = _row(
            await self._db.prepare(
                "SELECT * FROM qa_items WHERE canonical_key = ? AND scope_key = ?"
            )
            .bind(canonical_key, scope_key)
            .first()
        )
        return _qa_item(row) if row is not None else None

    async def save(self, item: QAItem) -> None:
        """Persist changes to an existing Q&A item."""
        await (
            self._db.prepare(
                "UPDATE qa_items SET canonical_question = ?, status = ?,"
                " current_version_id = ?, updated_at = ? WHERE id = ?"
            )
            .bind(
                item.canonical_question,
                item.status.value,
                item.current_version_id,
                _iso(item.updated_at),
                item.id,
            )
            .run()
        )


class D1QAVersionRepository:
    """D1 implementation of ``QAVersionRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, version: QAVersion) -> None:
        """Persist a new Q&A version."""
        await (
            self._db.prepare(
                "INSERT INTO qa_versions"
                " (id, qa_id, answer, authority, confidence, origin, created_by,"
                " supersedes_version_id, created_at, source_url, source_anchor, author)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                version.id,
                version.qa_id,
                version.answer,
                version.authority,
                version.confidence,
                version.origin,
                version.created_by,
                version.supersedes_version_id,
                _iso(version.created_at),
                version.source_url,
                version.source_anchor,
                version.author,
            )
            .run()
        )

    async def get(self, version_id: str) -> QAVersion | None:
        """Return a Q&A version by id, if present."""
        row = _row(
            await self._db.prepare("SELECT * FROM qa_versions WHERE id = ?")
            .bind(version_id)
            .first()
        )
        return _qa_version(row) if row is not None else None


class D1QAEvidenceRepository:
    """D1 implementation of ``QAEvidenceRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, evidence: QAEvidence) -> None:
        """Persist an evidence link."""
        await (
            self._db.prepare(
                "INSERT OR REPLACE INTO qa_evidence"
                " (qa_version_id, evidence_type, evidence_id) VALUES (?, ?, ?)"
            )
            .bind(
                evidence.qa_version_id,
                evidence.evidence_type.value,
                evidence.evidence_id,
            )
            .run()
        )


def _qa_item(row: dict[str, object]) -> QAItem:
    return QAItem(
        id=str(row["id"]),
        canonical_key=str(row["canonical_key"]),
        canonical_question=str(row["canonical_question"]),
        status=QAStatus(str(row["status"])),
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
        scope_key=str(row["scope_key"]),
        current_version_id=_opt_str(row["current_version_id"]),
    )


def _qa_version(row: dict[str, object]) -> QAVersion:
    confidence = row["confidence"]
    return QAVersion(
        id=str(row["id"]),
        qa_id=str(row["qa_id"]),
        answer=str(row["answer"]),
        authority=int(cast(int, row["authority"])),
        origin=str(row["origin"]),
        created_at=_dt(row["created_at"]),
        confidence=float(cast(float, confidence)) if confidence is not None else None,
        created_by=_opt_str(row["created_by"]),
        supersedes_version_id=_opt_str(row["supersedes_version_id"]),
        source_url=_opt_str(row["source_url"]),
        source_anchor=_opt_str(row.get("source_anchor")),
        author=_opt_str(row["author"]),
    )


class D1AiUsageRepository:
    """D1 implementation of ``AiUsageRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, day: str, neurons: float, calls: int) -> None:
        """Add an estimate to a day's running total."""
        await (
            self._db.prepare(
                "INSERT INTO ai_budget (day, neurons, calls, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(day) DO UPDATE SET"
                " neurons = neurons + excluded.neurons,"
                " calls = calls + excluded.calls,"
                " updated_at = excluded.updated_at"
            )
            .bind(day, neurons, calls, _iso(self._now()))
            .run()
        )

    def _now(self) -> datetime:
        return datetime.now(UTC)

    async def get(self, day: str) -> tuple[float, int]:
        """Return the day's ``(neurons, calls)`` so far."""
        row = _row(
            await self._db.prepare("SELECT neurons, calls FROM ai_budget WHERE day = ?")
            .bind(day)
            .first()
        )
        if row is None:
            return (0.0, 0)
        return (float(cast(float, row["neurons"])), int(cast(int, row["calls"])))


class D1CorrectionCommitStore:
    """D1 transactional implementation of correction decisions."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def approve(self, command: ApproveCorrectionCommand) -> QAVersion:
        """Commit version, current pointer, evidence, and feedback atomically."""
        version = command.version
        item = command.item
        evidence = command.evidence
        feedback = command.feedback
        await self._db.batch(
            [
                self._db.prepare(
                    "INSERT INTO qa_versions"
                    " (id, qa_id, answer, authority, confidence, origin, created_by,"
                    " supersedes_version_id, created_at, source_url, source_anchor,"
                    " author) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ).bind(
                    version.id,
                    version.qa_id,
                    version.answer,
                    version.authority,
                    version.confidence,
                    version.origin,
                    version.created_by,
                    version.supersedes_version_id,
                    _iso(version.created_at),
                    version.source_url,
                    version.source_anchor,
                    version.author,
                ),
                self._db.prepare(
                    "UPDATE qa_items SET canonical_question = ?, status = ?,"
                    " current_version_id = ?, updated_at = ? WHERE id = ?"
                ).bind(
                    item.canonical_question,
                    item.status.value,
                    item.current_version_id,
                    _iso(item.updated_at),
                    item.id,
                ),
                self._db.prepare(
                    "INSERT INTO qa_evidence"
                    " (qa_version_id, evidence_type, evidence_id) VALUES (?, ?, ?)"
                ).bind(
                    evidence.qa_version_id,
                    evidence.evidence_type.value,
                    evidence.evidence_id,
                ),
                self._db.prepare(
                    "UPDATE feedback SET status = ?, proposed_answer = ?,"
                    " admin_edited_answer = ?, resolved_at = ? WHERE id = ?"
                ).bind(
                    feedback.status.value,
                    feedback.proposed_answer,
                    feedback.admin_edited_answer,
                    _iso(feedback.resolved_at)
                    if feedback.resolved_at is not None
                    else None,
                    feedback.id,
                ),
            ]
        )
        return version

    async def reject(self, feedback: Feedback) -> Feedback:
        """Persist one rejected feedback decision."""
        await (
            self._db.prepare(
                "UPDATE feedback SET status = ?, resolved_at = ? WHERE id = ?"
            )
            .bind(
                feedback.status.value,
                _iso(feedback.resolved_at)
                if feedback.resolved_at is not None
                else None,
                feedback.id,
            )
            .run()
        )
        return feedback


class D1FeedbackRepository:
    """D1 implementation of ``FeedbackRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, feedback: Feedback) -> None:
        """Persist a new correction proposal."""
        await (
            self._db.prepare(
                "INSERT INTO feedback"
                " (id, bot_answer_id, qa_id, reporter_hash, reporter_chat_id,"
                " reporter_name, status, proposed_answer, admin_edited_answer,"
                " proposal_prompt_message_id, edit_prompt_message_id, created_at,"
                " proposed_at, resolved_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                feedback.id,
                feedback.bot_answer_id,
                feedback.qa_id,
                feedback.reporter_hash,
                feedback.reporter_chat_id,
                feedback.reporter_name,
                feedback.status.value,
                feedback.proposed_answer,
                feedback.admin_edited_answer,
                feedback.proposal_prompt_message_id,
                feedback.edit_prompt_message_id,
                _iso(feedback.created_at),
                _iso(feedback.proposed_at)
                if feedback.proposed_at is not None
                else None,
                _iso(feedback.resolved_at)
                if feedback.resolved_at is not None
                else None,
            )
            .run()
        )

    async def get(self, feedback_id: str) -> Feedback | None:
        """Return a correction proposal by id, if present."""
        row = _row(
            await self._db.prepare("SELECT * FROM feedback WHERE id = ?")
            .bind(feedback_id)
            .first()
        )
        return _feedback(row) if row is not None else None

    async def save(self, feedback: Feedback) -> None:
        """Persist changes to an existing correction proposal."""
        await (
            self._db.prepare(
                "UPDATE feedback SET qa_id = ?, reporter_hash = ?,"
                " reporter_chat_id = ?, reporter_name = ?, status = ?,"
                " proposed_answer = ?, admin_edited_answer = ?,"
                " proposal_prompt_message_id = ?, edit_prompt_message_id = ?,"
                " proposed_at = ?, resolved_at = ? WHERE id = ?"
            )
            .bind(
                feedback.qa_id,
                feedback.reporter_hash,
                feedback.reporter_chat_id,
                feedback.reporter_name,
                feedback.status.value,
                feedback.proposed_answer,
                feedback.admin_edited_answer,
                feedback.proposal_prompt_message_id,
                feedback.edit_prompt_message_id,
                _iso(feedback.proposed_at)
                if feedback.proposed_at is not None
                else None,
                _iso(feedback.resolved_at)
                if feedback.resolved_at is not None
                else None,
                feedback.id,
            )
            .run()
        )

    async def find_by_proposal_prompt(self, message_id: str) -> Feedback | None:
        """Return the feedback awaiting a proposal reply to a prompt message."""
        return await self._find("proposal_prompt_message_id", message_id)

    async def find_by_edit_prompt(self, message_id: str) -> Feedback | None:
        """Return the feedback awaiting an admin edit reply to a prompt message."""
        return await self._find("edit_prompt_message_id", message_id)

    async def list_between(self, start: datetime, end: datetime) -> list[Feedback]:
        """Return the correction proposals created in ``[start, end)``."""
        result = (
            await self._db.prepare(
                "SELECT * FROM feedback WHERE created_at >= ? AND created_at < ?"
                " ORDER BY created_at"
            )
            .bind(_iso(start), _iso(end))
            .run()
        )
        return [_feedback(row) for row in _rows(result)]

    async def _find(self, column: str, message_id: str) -> Feedback | None:
        row = _row(
            await self._db.prepare(f"SELECT * FROM feedback WHERE {column} = ?")
            .bind(message_id)
            .first()
        )
        return _feedback(row) if row is not None else None


def _feedback(row: dict[str, object]) -> Feedback:
    return Feedback(
        id=str(row["id"]),
        bot_answer_id=str(row["bot_answer_id"]),
        status=FeedbackStatus(str(row["status"])),
        created_at=_dt(row["created_at"]),
        qa_id=_opt_str(row["qa_id"]),
        reporter_hash=_opt_str(row["reporter_hash"]),
        reporter_chat_id=_opt_str(row["reporter_chat_id"]),
        reporter_name=_opt_str(row["reporter_name"]),
        proposed_answer=_opt_str(row["proposed_answer"]),
        admin_edited_answer=_opt_str(row["admin_edited_answer"]),
        proposal_prompt_message_id=_opt_str(row["proposal_prompt_message_id"]),
        edit_prompt_message_id=_opt_str(row["edit_prompt_message_id"]),
        proposed_at=_opt_dt(row["proposed_at"]),
        resolved_at=_opt_dt(row["resolved_at"]),
    )


class D1ReviewSource:
    """D1 implementation of ``ReviewSource`` for the human review report."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def list_current(self) -> list[ReviewItem]:
        """Return the current version of every active Q&A item.

        Each row carries the origin of the version it superseded, so the
        report can spot a renewal that overwrote a correction.
        """
        result = await self._db.prepare(
            "SELECT qi.canonical_key AS canonical_key,"
            " qi.canonical_question AS question, qi.scope_key AS scope,"
            " qi.status AS status,"
            " qv.answer AS answer, qv.origin AS origin, qv.created_at AS created_at,"
            " prev.origin AS superseded_origin"
            " FROM qa_items qi"
            " JOIN qa_versions qv ON qv.id = qi.current_version_id"
            " LEFT JOIN qa_versions prev ON prev.id = qv.supersedes_version_id"
            " WHERE qi.status IN ('active', 'under_review')"
        ).run()
        return [
            ReviewItem(
                canonical_key=str(row["canonical_key"]),
                question=str(row["question"]),
                scope=str(row["scope"]),
                answer=str(row["answer"]),
                origin=str(row["origin"]),
                created_at=_dt(row["created_at"]),
                status=str(row["status"]),
                superseded_origin=_opt_str(row["superseded_origin"]),
            )
            for row in _rows(result)
        ]


class D1ReviewerRepository:
    """D1 implementation of ``ReviewerRepository`` (one reviewer per scope)."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def get(self, scope: str) -> Reviewer | None:
        """Return the reviewer of a scope, if any."""
        row = _row(
            await self._db.prepare("SELECT * FROM reviewers WHERE scope = ?")
            .bind(scope)
            .first()
        )
        return _reviewer(row) if row is not None else None

    async def save(self, reviewer: Reviewer) -> None:
        """Create or replace the reviewer of a scope."""
        await (
            self._db.prepare(
                "INSERT INTO reviewers"
                " (scope, user_id, name, nominated_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(scope) DO UPDATE SET"
                " user_id = excluded.user_id, name = excluded.name,"
                " nominated_by = excluded.nominated_by,"
                " created_at = excluded.created_at"
            )
            .bind(
                reviewer.scope,
                reviewer.user_id,
                reviewer.name,
                reviewer.nominated_by,
                _iso(reviewer.created_at),
            )
            .run()
        )

    async def delete(self, scope: str) -> bool:
        """Remove the reviewer of a scope; return whether one existed."""
        if await self.get(scope) is None:
            return False
        await (
            self._db.prepare("DELETE FROM reviewers WHERE scope = ?").bind(scope).run()
        )
        return True

    async def all(self) -> list[Reviewer]:
        """Return every reviewer, global scope first."""
        result = await self._db.prepare(
            "SELECT * FROM reviewers"
            " ORDER BY CASE WHEN scope = 'global' THEN 0 ELSE 1 END, scope"
        ).run()
        return [_reviewer(row) for row in _rows(result)]


def _reviewer(row: dict[str, object]) -> Reviewer:
    return Reviewer(
        scope=str(row["scope"]),
        user_id=str(row["user_id"]),
        name=str(row["name"]),
        nominated_by=_opt_str(row["nominated_by"]),
        created_at=_dt(row["created_at"]),
    )


class D1ReviewerEventRepository:
    """D1 implementation of ``ReviewerEventRepository`` (admin report)."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, event: ReviewerEvent) -> ReviewerEvent:
        """Persist an event and return it."""
        await (
            self._db.prepare(
                "INSERT INTO reviewer_events"
                " (feedback_id, reviewer_user_id, reviewer_name, group_label,"
                "  question, action, approval_scope, created_at, reported)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                event.feedback_id,
                event.reviewer_user_id,
                event.reviewer_name,
                event.group_label,
                event.question,
                event.action,
                event.approval_scope,
                _iso(event.created_at),
                int(event.reported),
            )
            .run()
        )
        return event

    async def list_unreported(self) -> list[ReviewerEvent]:
        """Return events not yet included in an admin report."""
        result = await self._db.prepare(
            "SELECT * FROM reviewer_events WHERE reported = 0 ORDER BY created_at"
        ).run()
        return [_reviewer_event(row) for row in _rows(result)]

    async def mark_reported(self, feedback_ids: list[str]) -> None:
        """Mark the events of these feedback ids as reported."""
        if not feedback_ids:
            return
        placeholders = ", ".join("?" for _ in feedback_ids)
        await (
            self._db.prepare(
                f"UPDATE reviewer_events SET reported = 1"
                f" WHERE feedback_id IN ({placeholders})"
            )
            .bind(*feedback_ids)
            .run()
        )


def _reviewer_event(row: dict[str, object]) -> ReviewerEvent:
    return ReviewerEvent(
        id=_opt_int(row["id"]),
        feedback_id=str(row["feedback_id"]),
        action=str(row["action"]),
        created_at=_dt(row["created_at"]),
        reviewer_user_id=_opt_str(row["reviewer_user_id"]),
        reviewer_name=_opt_str(row["reviewer_name"]),
        group_label=_opt_str(row["group_label"]),
        question=_opt_str(row["question"]),
        approval_scope=_opt_str(row["approval_scope"]),
        reported=bool(row["reported"]),
    )


class D1ReportStateRepository:
    """D1 implementation of ``ReportStateRepository``."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def get_last_sent_at(self) -> datetime | None:
        """Return when the last admin report was sent, if ever."""
        row = _row(
            await self._db.prepare(
                "SELECT last_sent_at FROM report_state WHERE scope = 'admin'"
            ).first()
        )
        return _dt(row["last_sent_at"]) if row is not None else None

    async def set_last_sent_at(self, sent_at: datetime) -> None:
        """Record when the admin report was sent."""
        await (
            self._db.prepare(
                "INSERT INTO report_state (scope, last_sent_at) VALUES ('admin', ?)"
                " ON CONFLICT(scope) DO UPDATE SET"
                " last_sent_at = excluded.last_sent_at"
            )
            .bind(_iso(sent_at))
            .run()
        )
