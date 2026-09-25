# SPDX-License-Identifier: MIT
"""D1-backed repository implementations.

The D1 binding is asynchronous: ``db.prepare(sql).bind(...)`` then
``await stmt.first()`` or ``await stmt.run()``. Results are converted to Python
defensively because bindings can return Pyodide proxies.
"""

import json
import re
from datetime import UTC, datetime
from itertools import batched
from typing import cast

from knowledge_bot.domain.entities import (
    Attachment,
    BotAnswer,
    ChannelBinding,
    Conversation,
    DeliveryReceipt,
    Feedback,
    Message,
    MessagePairCandidate,
    QAEvidence,
    QAItem,
    QAVersion,
    Reviewer,
    ReviewerEvent,
    SearchProjectionEntry,
    Source,
    Space,
    TelegramInteraction,
)
from knowledge_bot.domain.enums import (
    AnswerMode,
    ClassificationStatus,
    ContentType,
    FeedbackStatus,
    IndexStatus,
    ProcessingStatus,
    ProjectionState,
    QAStatus,
)
from knowledge_bot.domain.policies import effective_message_authority
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.infrastructure.sql.protocol import (
    SqlDatabase,
    SqlResult,
    SqlStatement,
)
from knowledge_bot.ports.index import IndexableMessage, IndexableQA
from knowledge_bot.ports.lexical import LexicalMatch, LexicalRecord
from knowledge_bot.ports.repositories import DailyReportSnapshot
from knowledge_bot.ports.review import ReviewItem
from knowledge_bot.ports.transactions import ApproveCorrectionCommand

D1Result = SqlResult
D1Statement = SqlStatement
D1Database = SqlDatabase

_FTS_FILTER_COLUMNS = frozenset({"kind", "scope_key", "canonical_key"})
_FTS_TOKEN = re.compile(r"[\w]+", flags=re.UNICODE)
# D1 accepts a bounded number of statements per batch; lexical upsert writes two
# per record, so every batch stays at or below 100 statements.
_BATCH_CHUNK = 50


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression."""
    return " ".join(f'"{token}"' for token in _FTS_TOKEN.findall(query))


def _json_dumps(metadata: dict[str, object]) -> str:
    """Serialize lexical row metadata."""
    return json.dumps(metadata, separators=(",", ":"), default=str)


def _json_loads(value: object) -> dict[str, object]:
    """Deserialize lexical row metadata defensively."""
    if not isinstance(value, str):
        return {}
    try:
        payload = json.loads(value)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_int_authority(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


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


class D1DeliveryReceiptRepository:
    """D1 implementation of idempotent delivery receipts."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def get(
        self, object_type: str, object_id: str, channel: str
    ) -> DeliveryReceipt | None:
        """Return a prior successful delivery."""
        row = _row(
            await self._db.prepare(
                "SELECT * FROM delivery_receipts"
                " WHERE object_type = ? AND object_id = ? AND channel = ?"
            )
            .bind(object_type, object_id, channel)
            .first()
        )
        return _delivery_receipt(row) if row is not None else None

    async def add(self, receipt: DeliveryReceipt) -> None:
        """Persist one successful delivery."""
        await (
            self._db.prepare(
                "INSERT INTO delivery_receipts"
                " (id, object_type, object_id, channel, external_conversation_id,"
                " external_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                receipt.id,
                receipt.object_type,
                receipt.object_id,
                receipt.channel,
                receipt.external_conversation_id,
                receipt.external_message_id,
                _iso(receipt.created_at),
            )
            .run()
        )


class D1TelegramInteractionRepository:
    """D1 implementation of durable Telegram interactions."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def get(self, external_message_id: str) -> TelegramInteraction | None:
        """Return an interaction by prompt message id."""
        row = _row(
            await self._db.prepare(
                "SELECT * FROM telegram_interactions WHERE external_message_id = ?"
            )
            .bind(external_message_id)
            .first()
        )
        return _telegram_interaction(row) if row is not None else None

    async def add(self, interaction: TelegramInteraction) -> None:
        """Persist one prompt interaction."""
        await (
            self._db.prepare(
                "INSERT INTO telegram_interactions"
                " (external_message_id, interaction_type, object_id, principal_id,"
                " created_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?)"
            )
            .bind(
                interaction.external_message_id,
                interaction.interaction_type,
                interaction.object_id,
                interaction.principal_id,
                _iso(interaction.created_at),
                _iso(interaction.consumed_at)
                if interaction.consumed_at is not None
                else None,
            )
            .run()
        )

    async def consume(
        self,
        external_message_id: str,
        principal_id: str,
        consumed_at: datetime,
    ) -> TelegramInteraction | None:
        """Atomically consume one unused interaction for its principal."""
        row = _row(
            await self._db.prepare(
                "UPDATE telegram_interactions SET consumed_at = ?"
                " WHERE external_message_id = ? AND consumed_at IS NULL"
                " AND (principal_id IS NULL OR principal_id = ?)"
                " RETURNING *"
            )
            .bind(_iso(consumed_at), external_message_id, principal_id)
            .first()
        )
        return _telegram_interaction(row) if row is not None else None


def _delivery_receipt(row: dict[str, object]) -> DeliveryReceipt:
    return DeliveryReceipt(
        id=str(row["id"]),
        object_type=str(row["object_type"]),
        object_id=str(row["object_id"]),
        channel=str(row["channel"]),
        external_conversation_id=str(row["external_conversation_id"]),
        external_message_id=str(row["external_message_id"]),
        created_at=_dt(row["created_at"]),
    )


def _telegram_interaction(row: dict[str, object]) -> TelegramInteraction:
    return TelegramInteraction(
        external_message_id=str(row["external_message_id"]),
        interaction_type=str(row["interaction_type"]),
        object_id=str(row["object_id"]),
        principal_id=_opt_str(row["principal_id"]),
        created_at=_dt(row["created_at"]),
        consumed_at=_opt_dt(row["consumed_at"]),
    )


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

    async def list_recent(
        self, conversation_id: str, start: datetime, limit: int
    ) -> list[Message]:
        """Return recent messages in a conversation ordered by creation time."""
        result = await (
            self._db.prepare(
                "SELECT * FROM messages WHERE conversation_id = ?"
                " AND created_at >= ? ORDER BY created_at LIMIT ?"
            )
            .bind(conversation_id, _iso(start), limit)
            .run()
        )
        return [_message(row) for row in _rows(result)]

    async def list_recent_unpaired_questions(
        self,
        conversation_id: str,
        since: datetime,
        until: datetime,
        limit: int,
    ) -> list[Message]:
        """Return recent question candidates for temporal pairing.

        Args:
            conversation_id: The conversation to search.
            since: Oldest question creation time to consider.
            until: Newest question creation time (the answer's timestamp).
            limit: Maximum number of candidates.

        Returns:
            Questions never paired with an answer, newest first.
        """
        result = await (
            self._db.prepare(
                "SELECT * FROM messages WHERE conversation_id = ?"
                " AND intent_label = 'question' AND context_question IS NULL"
                " AND text IS NOT NULL AND created_at >= ? AND created_at <= ?"
                " ORDER BY created_at DESC LIMIT ?"
            )
            .bind(conversation_id, _iso(since), _iso(until), limit)
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


class D1MessagePairCandidateRepository:
    """D1 implementation of non-authoritative listener pair candidates."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def add(self, candidate: MessagePairCandidate) -> bool:
        """Persist one candidate idempotently."""
        try:
            await (
                self._db.prepare(
                    "INSERT INTO message_pair_candidates"
                    " (id, conversation_id, question_message_id, answer_message_id,"
                    " confidence, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                )
                .bind(
                    candidate.id,
                    candidate.conversation_id,
                    candidate.question_message_id,
                    candidate.answer_message_id,
                    candidate.confidence,
                    candidate.source,
                    _iso(candidate.created_at),
                )
                .run()
            )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                return False
            raise
        return True

    async def list_for_conversation(
        self, conversation_id: str
    ) -> list[MessagePairCandidate]:
        """Return candidates for one conversation."""
        result = await (
            self._db.prepare(
                "SELECT * FROM message_pair_candidates"
                " WHERE conversation_id = ? ORDER BY created_at"
            )
            .bind(conversation_id)
            .run()
        )
        return [_pair_candidate(row) for row in _rows(result)]


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
                " qa_version_id, sources_json, rendered_text, source_details_json,"
                " created_at, request_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
                answer.rendered_text or answer.answer,
                answer.source_details_json,
                _iso(answer.created_at),
                answer.request_id,
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

    async def get_by_request_id(self, request_id: str) -> BotAnswer | None:
        """Return the answer previously created for an idempotency key."""
        row = _row(
            await self._db.prepare("SELECT * FROM bot_answers WHERE request_id = ?")
            .bind(request_id)
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


def _pair_candidate(row: dict[str, object]) -> MessagePairCandidate:
    return MessagePairCandidate(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        question_message_id=str(row["question_message_id"]),
        answer_message_id=str(row["answer_message_id"]),
        confidence=float(cast(float, row["confidence"])),
        source=str(row["source"]),
        created_at=_dt(row["created_at"]),
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
        request_id=_opt_str(row.get("request_id")),
        confidence=float(cast(float, row["confidence"]))
        if row["confidence"] is not None
        else None,
        qa_version_id=_opt_str(row["qa_version_id"]),
        sources_json=str(row["sources_json"]),
        rendered_text=str(row.get("rendered_text") or row["answer"]),
        source_details_json=str(row.get("source_details_json") or "[]"),
    )


def _message_authority(row: dict[str, object]) -> int:
    """Return the connector-declared source authority for an indexed message."""
    return int(cast(int, row["source_authority"]))


class D1SearchProjectionRepository:
    """D1 implementation of durable search projection state."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def reserve(
        self,
        vector_id: str,
        kind: str,
        object_id: str,
        version_id: str | None,
        updated_at: datetime,
    ) -> None:
        """Reserve a stable vector before projection."""
        await (
            self._db.prepare(
                "INSERT INTO search_projection"
                " (vector_id, kind, object_id, version_id, state, updated_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)"
                " ON CONFLICT(vector_id) DO UPDATE SET kind = excluded.kind,"
                " object_id = excluded.object_id,"
                " version_id = excluded.version_id,"
                " state = 'pending', last_error = NULL,"
                " updated_at = excluded.updated_at"
            )
            .bind(vector_id, kind, object_id, version_id, _iso(updated_at))
            .run()
        )

    async def mark_active(
        self, vector_id: str, version_id: str | None, updated_at: datetime
    ) -> None:
        """Mark a projection active."""
        await (
            self._db.prepare(
                "UPDATE search_projection SET version_id = ?, state = 'active',"
                " last_error = NULL, updated_at = ? WHERE vector_id = ?"
            )
            .bind(version_id, _iso(updated_at), vector_id)
            .run()
        )

    async def mark_failed(
        self,
        vector_id: str,
        version_id: str | None,
        error_code: str,
        updated_at: datetime,
    ) -> None:
        """Mark a projection failed with a safe error code."""
        await (
            self._db.prepare(
                "UPDATE search_projection SET version_id = ?, state = 'failed',"
                " last_error = ?, updated_at = ? WHERE vector_id = ?"
            )
            .bind(version_id, error_code, _iso(updated_at), vector_id)
            .run()
        )

    async def get(self, vector_id: str) -> SearchProjectionEntry | None:
        """Return one projection entry."""
        row = _row(
            await self._db.prepare(
                "SELECT * FROM search_projection WHERE vector_id = ?"
            )
            .bind(vector_id)
            .first()
        )
        return _projection_entry(row) if row is not None else None

    async def list_by_state(
        self, states: list[ProjectionState], limit: int
    ) -> list[SearchProjectionEntry]:
        """Return a bounded repair batch."""
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        result = self._db.prepare(
            f"SELECT * FROM search_projection WHERE state IN ({placeholders})"
            " ORDER BY updated_at, vector_id LIMIT ?"
        )
        for state in states:
            result = result.bind(state.value)
        return [_projection_entry(row) for row in _rows(await result.bind(limit).run())]

    async def list_vector_ids(self) -> list[str]:
        """Return every projected vector id."""
        result = await self._db.prepare(
            "SELECT vector_id FROM search_projection ORDER BY vector_id"
        ).run()
        return [str(row["vector_id"]) for row in _rows(result)]

    async def delete(self, vector_ids: list[str]) -> None:
        """Remove deleted vector ids from the manifest, in bounded batches."""
        for chunk in batched(vector_ids, _BATCH_CHUNK, strict=False):
            await self._db.batch(
                [
                    self._db.prepare(
                        "DELETE FROM search_projection WHERE vector_id = ?"
                    ).bind(vector_id)
                    for vector_id in chunk
                ]
            )

    async def clear(self) -> None:
        """Clear the projection manifest."""
        await self._db.prepare("DELETE FROM search_projection").run()


def _projection_entry(row: dict[str, object]) -> SearchProjectionEntry:
    """Map a projection row to its domain value."""
    return SearchProjectionEntry(
        vector_id=str(row["vector_id"]),
        kind=str(row["kind"]),
        object_id=str(row["object_id"]),
        version_id=_opt_str(row.get("version_id")),
        state=ProjectionState(str(row.get("state") or "active")),
        updated_at=_dt(row["updated_at"]),
        last_error=_opt_str(row.get("last_error")),
    )


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
                " WHERE qi.status = 'active' AND qi.current_version_id = qv.id"
                " AND qv.id = ?"
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

    async def get_current_qa_by_item_id(self, qa_item_id: str) -> IndexableQA | None:
        """Return the current active Q&A version for one item."""
        row = _row(
            await self._db.prepare(
                "SELECT qi.id AS item_id, qv.id AS version_id,"
                " qi.canonical_question AS question, qv.answer AS answer,"
                " qv.authority AS authority, qi.canonical_key AS canonical_key,"
                " qv.source_url AS source_url, qv.source_anchor AS source_anchor,"
                " qv.author AS author, qv.created_at AS created_at,"
                " qi.scope_key AS scope_key FROM qa_versions qv"
                " JOIN qa_items qi ON qi.id = qv.qa_id"
                " WHERE qi.status = 'active' AND qi.current_version_id = qv.id"
                " AND qi.id = ?"
            )
            .bind(qa_item_id)
            .first()
        )
        if row is None:
            return None
        return self._qa_record(row)

    @staticmethod
    def _qa_record(row: dict[str, object]) -> IndexableQA:
        """Map a Q&A row to an indexable record."""
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

    async def list_legacy_vector_ids(self) -> list[str]:
        """Return old version, raw-message, and pair vector ids."""
        rows = _rows(
            await self._db.prepare(
                "SELECT id FROM qa_versions UNION ALL"
                " SELECT id FROM messages UNION ALL"
                " SELECT 'pair:' || id FROM message_pair_candidates"
            ).run()
        )
        return [str(row["id"]) for row in rows]

    async def get_indexable_message(self, message_id: str) -> IndexableMessage | None:
        """Return one eligible message for repair."""
        result = (
            await self._db.prepare(
                "SELECT m.id, m.source_id, m.conversation_id, m.text,"
                " m.sender_hash, m.sender_name, m.sent_at, m.context_question,"
                " m.sender_authority, c.space_id, s.source_type AS source_kind,"
                " s.authority AS source_authority FROM messages m"
                " JOIN sources s ON s.id = m.source_id"
                " JOIN conversations c ON c.id = m.conversation_id"
                " WHERE m.id = ? AND m.text IS NOT NULL AND m.text != ''"
                " AND m.index_status IN ('indexed', 'pending', 'failed')"
            )
            .bind(message_id)
            .run()
        )
        rows = _rows(result)
        return self._message_record(rows[0]) if rows else None

    async def list_messages(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableMessage]:
        """Return the messages with text to index, in id-order batches."""
        query = (
            "SELECT m.id, m.source_id, m.conversation_id, m.text,"
            " m.sender_hash, m.sender_name, m.sent_at, m.context_question,"
            " m.sender_authority, c.space_id, s.source_type AS source_kind,"
            " s.authority AS source_authority"
            " FROM messages m JOIN sources s ON s.id = m.source_id"
            " JOIN conversations c ON c.id = m.conversation_id"
            " WHERE m.text IS NOT NULL AND m.text != ''"
            " AND m.index_status IN ('indexed', 'pending', 'failed')"
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
        return [self._message_record(row) for row in _rows(result)]

    @staticmethod
    def _message_record(row: dict[str, object]) -> IndexableMessage:
        """Map a message row to an indexable record."""
        return IndexableMessage(
            message_id=str(row["id"]),
            text=str(row["text"]),
            source_kind=str(row["source_kind"]),
            authority=effective_message_authority(
                int(cast(int, row["source_authority"])),
                _opt_int(row.get("sender_authority")),
            ),
            conversation_id=str(row["conversation_id"]),
            scope_key=scope_for_space(str(row["space_id"]))
            if row.get("space_id")
            else GLOBAL_SCOPE,
            author=_sender_label(row["sender_name"], row["sender_hash"]),
            date=_datetime_part(row["sent_at"]),
            question=_opt_str(row.get("context_question")),
        )


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
                    "INSERT INTO qa_items"
                    " (id, canonical_key, canonical_question, status,"
                    " current_version_id, created_at, updated_at, scope_key)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET"
                    " canonical_question = excluded.canonical_question,"
                    " status = excluded.status,"
                    " current_version_id = excluded.current_version_id,"
                    " updated_at = excluded.updated_at"
                ).bind(
                    item.id,
                    item.canonical_key,
                    item.canonical_question,
                    item.status.value,
                    item.current_version_id,
                    _iso(item.created_at),
                    _iso(item.updated_at),
                    item.scope_key,
                ),
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
                " reporter_name, reporter_principal_id, origin_space_id, status,"
                " proposed_answer, admin_edited_answer,"
                " proposal_prompt_message_id, edit_prompt_message_id, created_at,"
                " proposed_at, resolved_at, reviewer_delivery_failed_at,"
                " reviewer_escalated_at, reviewer_destination)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                feedback.id,
                feedback.bot_answer_id,
                feedback.qa_id,
                feedback.reporter_hash,
                feedback.reporter_chat_id,
                feedback.reporter_name,
                feedback.reporter_principal_id,
                feedback.origin_space_id,
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
                _iso(feedback.reviewer_delivery_failed_at)
                if feedback.reviewer_delivery_failed_at is not None
                else None,
                _iso(feedback.reviewer_escalated_at)
                if feedback.reviewer_escalated_at is not None
                else None,
                feedback.reviewer_destination,
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
                " reporter_chat_id = ?, reporter_name = ?, reporter_principal_id = ?,"
                " origin_space_id = ?, status = ?, proposed_answer = ?,"
                " admin_edited_answer = ?,"
                " proposal_prompt_message_id = ?, edit_prompt_message_id = ?,"
                " proposed_at = ?, resolved_at = ?, reviewer_delivery_failed_at = ?,"
                " reviewer_escalated_at = ?, reviewer_destination = ? WHERE id = ?"
            )
            .bind(
                feedback.qa_id,
                feedback.reporter_hash,
                feedback.reporter_chat_id,
                feedback.reporter_name,
                feedback.reporter_principal_id,
                feedback.origin_space_id,
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
                _iso(feedback.reviewer_delivery_failed_at)
                if feedback.reviewer_delivery_failed_at is not None
                else None,
                _iso(feedback.reviewer_escalated_at)
                if feedback.reviewer_escalated_at is not None
                else None,
                feedback.reviewer_destination,
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

    async def list_escalatable(self) -> list[Feedback]:
        """Return pending reviews whose reviewer delivery has failed."""
        result = await (
            self._db.prepare(
                "SELECT * FROM feedback WHERE status = ?"
                " AND reviewer_delivery_failed_at IS NOT NULL"
                " AND reviewer_escalated_at IS NULL"
            )
            .bind(FeedbackStatus.PENDING_REVIEW.value)
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
        reporter_principal_id=_opt_str(row.get("reporter_principal_id")),
        origin_space_id=_opt_str(row.get("origin_space_id")),
        proposed_answer=_opt_str(row["proposed_answer"]),
        admin_edited_answer=_opt_str(row["admin_edited_answer"]),
        proposal_prompt_message_id=_opt_str(row["proposal_prompt_message_id"]),
        edit_prompt_message_id=_opt_str(row["edit_prompt_message_id"]),
        proposed_at=_opt_dt(row["proposed_at"]),
        resolved_at=_opt_dt(row["resolved_at"]),
        reviewer_delivery_failed_at=_opt_dt(row.get("reviewer_delivery_failed_at")),
        reviewer_escalated_at=_opt_dt(row.get("reviewer_escalated_at")),
        reviewer_destination=_opt_str(row.get("reviewer_destination")),
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
                " (scope, principal_id, name, nominated_by_principal_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(scope) DO UPDATE SET"
                " principal_id = excluded.principal_id, name = excluded.name,"
                " nominated_by_principal_id = excluded.nominated_by_principal_id,"
                " created_at = excluded.created_at"
            )
            .bind(
                reviewer.scope,
                reviewer.principal_id,
                reviewer.name,
                reviewer.nominated_by_principal_id,
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
        principal_id=str(row["principal_id"]),
        name=str(row["name"]),
        nominated_by_principal_id=_opt_str(row["nominated_by_principal_id"]),
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
                " (feedback_id, reviewer_principal_id, reviewer_name, group_label,"
                "  question, action, approval_scope, created_at, reported)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(
                event.feedback_id,
                event.reviewer_principal_id,
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
        reviewer_principal_id=_opt_str(row["reviewer_principal_id"]),
        reviewer_name=_opt_str(row["reviewer_name"]),
        group_label=_opt_str(row["group_label"]),
        question=_opt_str(row["question"]),
        approval_scope=_opt_str(row["approval_scope"]),
        reported=bool(row["reported"]),
    )


class D1DailyReportSource:
    """D1 source for deterministic daily report counters."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def collect(self, start: datetime, end: datetime) -> DailyReportSnapshot:
        """Collect one report window without calling an AI model."""
        modes = _rows(
            await self._db.prepare(
                "SELECT answer_mode, COUNT(*) AS n FROM bot_answers"
                " WHERE created_at >= ? AND created_at < ? GROUP BY answer_mode"
            )
            .bind(_iso(start), _iso(end))
            .run()
        )
        counts = {str(row["answer_mode"]): _count(row) for row in modes}
        feedback = _count(
            _row(
                await self._db.prepare(
                    "SELECT COUNT(*) AS n FROM feedback"
                    " WHERE created_at >= ? AND created_at < ?"
                )
                .bind(_iso(start), _iso(end))
                .first()
            )
        )
        listener = _rows(
            await self._db.prepare(
                "SELECT classification_status, index_status, COUNT(*) AS n"
                " FROM messages WHERE created_at >= ? AND created_at < ?"
                " GROUP BY classification_status, index_status"
            )
            .bind(_iso(start), _iso(end))
            .run()
        )
        status_counts: dict[str, int] = {}
        for row in listener:
            status_counts[str(row["classification_status"])] = status_counts.get(
                str(row["classification_status"]), 0
            ) + _count(row)
        correction_rows = _rows(
            await self._db.prepare(
                "SELECT status, COUNT(*) AS n FROM feedback"
                " WHERE created_at >= ? AND created_at < ? GROUP BY status"
            )
            .bind(_iso(start), _iso(end))
            .run()
        )
        corrections = {str(row["status"]): _count(row) for row in correction_rows}
        audit_rows = _rows(
            await self._db.prepare(
                "SELECT reviewer_name, group_label, action, approval_scope"
                " FROM reviewer_events WHERE created_at >= ? AND created_at < ?"
                " ORDER BY created_at"
            )
            .bind(_iso(start), _iso(end))
            .run()
        )
        audit = tuple(
            f"{row.get('reviewer_name') or 'reviewer'}"
            f" · {row.get('group_label') or 'global'}"
            f" · {row.get('action') or 'resolved'}"
            for row in audit_rows
        )
        approved_local = sum(
            1
            for row in audit_rows
            if (
                str(row.get("action")) == "approve_group"
                or (
                    str(row.get("action")) == "edited_approved"
                    and str(row.get("approval_scope")) != "global"
                )
            )
        )
        approved_global = sum(
            1
            for row in audit_rows
            if str(row.get("action")) == "approve_global"
            or (
                str(row.get("action")) == "edited_approved"
                and str(row.get("approval_scope")) == "global"
            )
        )
        divergence = _count(
            _row(
                await self._db.prepare(
                    "SELECT COUNT(DISTINCT refreshed.qa_id) AS n"
                    " FROM qa_versions refreshed"
                    " JOIN qa_versions current"
                    " ON current.id = refreshed.supersedes_version_id"
                    " WHERE refreshed.origin = 'web_seed'"
                    " AND current.origin = 'human_approved'"
                    " AND refreshed.created_at >= ? AND refreshed.created_at < ?"
                )
                .bind(_iso(start), _iso(end))
                .first()
            )
        )
        projection_rows = _rows(
            await self._db.prepare(
                "SELECT state, COUNT(*) AS n FROM search_projection GROUP BY state"
            ).run()
        )
        projection_counts = {str(row["state"]): _count(row) for row in projection_rows}
        return DailyReportSnapshot(
            addressed_total=sum(counts.values()),
            direct=counts.get("direct_qa", 0),
            synthesis=counts.get("synthesis", 0),
            abstention=counts.get("abstention", 0),
            unavailable=counts.get("unavailable", 0),
            flagged=feedback,
            background_questions=status_counts.get("question", 0),
            background_paired=_count(
                _row(
                    await self._db.prepare(
                        "SELECT COUNT(*) AS n FROM messages"
                        " WHERE context_question IS NOT NULL"
                        " AND created_at >= ? AND created_at < ?"
                    )
                    .bind(_iso(start), _iso(end))
                    .first()
                )
            ),
            messages_stored=sum(status_counts.values()),
            evidence_indexed=sum(
                _count(row) for row in listener if str(row["index_status"]) == "indexed"
            ),
            non_evidence=sum(
                _count(row)
                for row in listener
                if str(row["index_status"]) in {"not_eligible", "not_indexed"}
            ),
            deferred=status_counts.get("deferred_budget", 0),
            failures=status_counts.get("failed", 0),
            corrections_proposed=corrections.get("pending_review", 0),
            approved_local=approved_local,
            approved_global=approved_global,
            rejected=corrections.get("rejected", 0),
            audit_labels=audit,
            seed_divergences=divergence,
            projection_pending=projection_counts.get("pending", 0),
            projection_failed=projection_counts.get("failed", 0),
        )


class D1DailyReportStateRepository:
    """D1 implementation of daily report send state."""

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def get(self, key: str) -> datetime | None:
        """Return the last successful report time."""
        row = _row(
            await self._db.prepare(
                "SELECT last_sent_at FROM daily_report_state WHERE key = ?"
            )
            .bind(key)
            .first()
        )
        return _opt_dt(row["last_sent_at"]) if row is not None else None

    async def set(self, key: str, sent_at: datetime) -> None:
        """Record a successful report time."""
        await (
            self._db.prepare(
                "INSERT INTO daily_report_state (key, last_sent_at) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET last_sent_at = excluded.last_sent_at"
            )
            .bind(key, _iso(sent_at))
            .run()
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


class D1LexicalIndex:
    """D1/SQLite FTS5 projection of searchable question texts.

    Rows are keyed by the stable vector id so the lexical projection stays in
    lockstep with the vector projection lifecycle.
    """

    def __init__(self, database: D1Database) -> None:
        """Wrap a D1 database binding."""
        self._db = database

    async def upsert(self, records: list[LexicalRecord]) -> None:
        """Replace the lexical row of every given vector id."""
        for chunk in batched(records, _BATCH_CHUNK, strict=False):
            statements = []
            for record in chunk:
                statements.append(
                    self._db.prepare("DELETE FROM search_fts WHERE vector_id = ?").bind(
                        record.id
                    )
                )
                statements.append(
                    self._db.prepare(
                        "INSERT INTO search_fts"
                        " (vector_id, kind, scope_key, canonical_key, authority,"
                        "  metadata_json, question_text)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    ).bind(
                        record.id,
                        str(record.metadata.get("kind", "")),
                        str(record.metadata.get("scope_key", "")),
                        _opt_str(record.metadata.get("canonical_key")),
                        _as_int_authority(record.metadata.get("authority")),
                        _json_dumps(record.metadata),
                        record.text,
                    )
                )
            await self._db.batch(statements)

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        filters: dict[str, object] | None = None,
    ) -> list[LexicalMatch]:
        """Return BM25-ranked matches, optionally filtered by metadata."""
        match_query = _fts_query(query)
        if not match_query:
            return []
        clauses = ["search_fts MATCH ?"]
        parameters: list[object] = [match_query]
        for key, value in (filters or {}).items():
            if key not in _FTS_FILTER_COLUMNS:
                raise ValueError(f"unsupported lexical filter: {key}")  # noqa: TRY003
            clauses.append(f"{key} = ?")
            parameters.append(value)
        parameters.append(top_k)
        result = await (
            self._db.prepare(
                "SELECT vector_id, metadata_json FROM search_fts"
                f" WHERE {' AND '.join(clauses)}"
                " ORDER BY bm25(search_fts) LIMIT ?"
            )
            .bind(*parameters)
            .run()
        )
        return [
            LexicalMatch(
                id=str(row["vector_id"]),
                metadata=_json_loads(row["metadata_json"]),
            )
            for row in _rows(result)
        ]

    async def delete(self, ids: list[str]) -> None:
        """Delete lexical rows by their stable projection ids."""
        for chunk in batched(ids, _BATCH_CHUNK, strict=False):
            await self._db.batch(
                [
                    self._db.prepare("DELETE FROM search_fts WHERE vector_id = ?").bind(
                        vector_id
                    )
                    for vector_id in chunk
                ]
            )
