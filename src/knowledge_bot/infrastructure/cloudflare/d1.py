# SPDX-License-Identifier: MIT
"""D1-backed repository implementations.

The D1 binding is asynchronous: ``db.prepare(sql).bind(...)`` then
``await stmt.first()`` or ``await stmt.run()``. Results are converted to Python
defensively because bindings can return Pyodide proxies.
"""

from datetime import UTC, datetime
from typing import Protocol, cast

from knowledge_bot.domain.entities import (
    Attachment,
    BotAnswer,
    Conversation,
    Feedback,
    Message,
    QAEvidence,
    QAItem,
    QAVersion,
    Source,
)
from knowledge_bot.domain.enums import (
    AnswerMode,
    ContentType,
    FeedbackStatus,
    ProcessingStatus,
    QAOrigin,
    QAStatus,
    SourceType,
)
from knowledge_bot.ports.index import IndexableMessage, IndexableQA
from knowledge_bot.ports.review import ReviewItem


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
                " authority, is_mutable, created_at, scope)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
                source.scope,
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
            scope=str(row["scope"]),
        )

    async def save(self, source: Source) -> None:
        """Persist changes to an existing source."""
        await (
            self._db.prepare(
                "UPDATE sources SET source_type = ?, external_ref = ?, title = ?,"
                " canonical_url = ?, authority = ?, is_mutable = ?, scope = ?"
                " WHERE id = ?"
            )
            .bind(
                source.source_type.value,
                source.external_ref,
                source.title,
                source.canonical_url,
                source.authority,
                int(source.is_mutable),
                source.scope,
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
                " sender_name, sender_is_admin, sent_at, text, content_type,"
                " reply_to_message_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                message.id,
                message.source_id,
                message.conversation_id,
                message.external_id,
                message.sender_hash,
                message.sender_name,
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
        sender_name=_opt_str(row["sender_name"]),
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


_MESSAGE_AUTHORITY: dict[str, int] = {
    "telegram": 40,
    "whatsapp_import": 50,
    "web_seed": 90,
    "admin": 100,
}


def _message_authority(source_type: str) -> int:
    return _MESSAGE_AUTHORITY.get(source_type, 0)


class D1SearchIndexSource:
    """D1 implementation of ``SearchIndexSource`` for reindexing."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def list_qa(self) -> list[IndexableQA]:
        """Return the active Q&A versions to index.

        Each version is cited from its own origin: a web snapshot entry keeps
        its anchored URL, an approved correction cites its author.
        """
        result = await self._db.prepare(
            "SELECT qv.id AS version_id, qi.canonical_question AS question,"
            " qv.answer AS answer, qv.authority AS authority,"
            " qi.canonical_key AS anchor, qv.source_url AS source_url,"
            " qv.author AS author, qv.created_at AS created_at, qi.scope AS scope"
            " FROM qa_versions qv"
            " JOIN qa_items qi ON qi.id = qv.qa_id"
            " WHERE qi.status = 'active' AND qi.current_version_id = qv.id"
        ).run()
        return [
            IndexableQA(
                version_id=str(row["version_id"]),
                question=str(row["question"]),
                answer=str(row["answer"]),
                authority=int(cast(int, row["authority"])),
                anchor=_opt_str(row["anchor"]),
                url=_exact_url(row["source_url"], row["anchor"]),
                date=_date_part(row["created_at"]),
                author=_opt_str(row["author"]),
                scope=str(row["scope"]),
            )
            for row in _rows(result)
        ]

    async def list_messages(self) -> list[IndexableMessage]:
        """Return the messages with text to index."""
        result = await self._db.prepare(
            "SELECT id, source_id, conversation_id, text,"
            " sender_hash, sender_name, sent_at"
            " FROM messages WHERE text IS NOT NULL AND text != ''"
        ).run()
        return [
            IndexableMessage(
                message_id=str(row["id"]),
                text=str(row["text"]),
                source_type=str(row["source_id"]),
                authority=_message_authority(str(row["source_id"])),
                conversation_id=str(row["conversation_id"]),
                author=_sender_label(row["sender_name"], row["sender_hash"]),
                date=_datetime_part(row["sent_at"]),
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
                " created_at, updated_at, scope) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                item.id,
                item.canonical_key,
                item.canonical_question,
                item.status.value,
                item.current_version_id,
                _iso(item.created_at),
                _iso(item.updated_at),
                item.scope,
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
        scope: str = "global",
    ) -> QAItem | None:
        """Return a Q&A item by canonical key within a scope, if present."""
        row = _row(
            await self._db.prepare(
                "SELECT * FROM qa_items WHERE canonical_key = ? AND scope = ?"
            )
            .bind(canonical_key, scope)
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
                " supersedes_version_id, created_at, source_url, author)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                version.id,
                version.qa_id,
                version.answer,
                version.authority,
                version.confidence,
                version.origin.value,
                version.created_by,
                version.supersedes_version_id,
                _iso(version.created_at),
                version.source_url,
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
        scope=str(row["scope"]),
        current_version_id=_opt_str(row["current_version_id"]),
    )


def _qa_version(row: dict[str, object]) -> QAVersion:
    confidence = row["confidence"]
    return QAVersion(
        id=str(row["id"]),
        qa_id=str(row["qa_id"]),
        answer=str(row["answer"]),
        authority=int(cast(int, row["authority"])),
        origin=QAOrigin(str(row["origin"])),
        created_at=_dt(row["created_at"]),
        confidence=float(cast(float, confidence)) if confidence is not None else None,
        created_by=_opt_str(row["created_by"]),
        supersedes_version_id=_opt_str(row["supersedes_version_id"]),
        source_url=_opt_str(row["source_url"]),
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
            " qi.canonical_question AS question, qi.scope AS scope,"
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
