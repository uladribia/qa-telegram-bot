# SPDX-License-Identifier: MIT
"""D1-backed repository implementations.

The D1 binding is asynchronous: ``db.prepare(sql).bind(...)`` then
``await stmt.first()`` or ``await stmt.run()``. Results are converted to Python
defensively because bindings can return Pyodide proxies.
"""

from datetime import datetime
from typing import Protocol, cast

from knowledge_bot.domain.entities import (
    Attachment,
    BotAnswer,
    Conversation,
    Message,
    Source,
)
from knowledge_bot.domain.enums import (
    AnswerMode,
    ContentType,
    ProcessingStatus,
    SourceType,
)


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


def _opt_dt(value: object) -> datetime | None:
    return None if value is None else _dt(value)


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
                " authority, is_mutable, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                source.id,
                source.source_type.value,
                source.external_ref,
                source.title,
                source.canonical_url,
                source.authority,
                int(source.is_mutable),
                _iso(source.created_at),
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
            source_type=SourceType(str(row["source_type"])),
            authority=int(cast(int, row["authority"])),
            created_at=_dt(row["created_at"]),
            external_ref=_opt_str(row["external_ref"]),
            title=_opt_str(row["title"]),
            canonical_url=_opt_str(row["canonical_url"]),
            is_mutable=bool(row["is_mutable"]),
        )

    async def save(self, source: Source) -> None:
        """Persist changes to an existing source."""
        await (
            self._db.prepare(
                "UPDATE sources SET source_type = ?, external_ref = ?, title = ?,"
                " canonical_url = ?, authority = ?, is_mutable = ? WHERE id = ?"
            )
            .bind(
                source.source_type.value,
                source.external_ref,
                source.title,
                source.canonical_url,
                source.authority,
                int(source.is_mutable),
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
                " (id, source_id, external_id, title, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
            )
            .bind(
                conversation.id,
                conversation.source_id,
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
            created_at=_dt(row["created_at"]),
            external_id=_opt_str(row["external_id"]),
            title=_opt_str(row["title"]),
        )

    async def save(self, conversation: Conversation) -> None:
        """Persist changes to an existing conversation."""
        await (
            self._db.prepare(
                "UPDATE conversations SET source_id = ?, external_id = ?,"
                " title = ? WHERE id = ?"
            )
            .bind(
                conversation.source_id,
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
                " sender_is_admin,"
                " sent_at, text, content_type, reply_to_message_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                message.id,
                message.source_id,
                message.conversation_id,
                message.external_id,
                message.sender_hash,
                int(message.sender_is_admin),
                _iso(message.sent_at),
                message.text,
                message.content_type.value,
                message.reply_to_message_id,
                _iso(message.created_at),
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
                " (id, conversation_id, user_message_id, telegram_bot_message_id,"
                " question, answer,"
                " answer_mode, confidence, qa_version_id, sources_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                answer.id,
                answer.conversation_id,
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


def _message(row: dict[str, object]) -> Message:
    return Message(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        conversation_id=str(row["conversation_id"]),
        content_type=ContentType(str(row["content_type"])),
        sent_at=_dt(row["sent_at"]),
        created_at=_dt(row["created_at"]),
        sender_is_admin=bool(row["sender_is_admin"]),
        external_id=_opt_str(row["external_id"]),
        sender_hash=_opt_str(row["sender_hash"]),
        text=_opt_str(row["text"]),
        reply_to_message_id=_opt_str(row["reply_to_message_id"]),
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
