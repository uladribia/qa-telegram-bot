# SPDX-License-Identifier: MIT
"""WhatsApp chat export parser (spec §8).

Parses a plain-text chat export into normalized messages. Attachments are
recorded as metadata only; their content is never read or downloaded.

A generic media placeholder (for example ``<Multimedia omès>``) carries no kind,
so it is classified as ``ContentType.UNKNOWN``. Placeholders that name the kind
(``image omitted``) or a file name with a known extension are classified
precisely.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

from knowledge_bot.contracts.messages import AttachmentRef, NormalizedMessage
from knowledge_bot.domain.enums import ContentType

# Android: ``19/09/26, 09:32 - Nom: Missatge``
_ANDROID_HEADER = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}), "
    r"(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?[Mm]\.?)?) - (.*)$"
)
# iOS: ``[9/3/26, 2:29:14 PM] Nom: Missatge`` (the author is optional: system lines).
_IOS_HEADER = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}), "
    r"(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?[Mm]\.?)?)\]\s*(?:-\s*)?(.*)$"
)
_LOOKS_LIKE_HEADER = re.compile(r"^\[?\d{1,2}/\d{1,2}/\d{2,4}")

_MEDIA_PLACEHOLDER = re.compile(
    r"<[^>]*(?:multimedia|m[eè]dia|adjunt|attachment)[^>]*>"
    r"|\b(?:image|video|audio|document|sticker|gif)\s+omitted\b",
    re.IGNORECASE,
)
_MEDIA_FILE_NAME = re.compile(
    r"\b(?:IMG|VID|AUD|PTT|DOC|STK)-\d{8}-WA\d+\.[A-Za-z0-9]+\b",
    re.IGNORECASE,
)

_KIND_WORDS: tuple[tuple[str, ContentType], ...] = (
    ("video", ContentType.VIDEO),
    ("v\u00eddeo", ContentType.VIDEO),
    ("audio", ContentType.AUDIO),
    ("\u00e0udio", ContentType.AUDIO),
    ("document", ContentType.DOCUMENT),
    ("image", ContentType.IMAGE),
    ("imatge", ContentType.IMAGE),
    ("foto", ContentType.IMAGE),
    ("sticker", ContentType.IMAGE),
    ("gif", ContentType.IMAGE),
)

_EXTENSION_KINDS: dict[str, ContentType] = {
    "jpg": ContentType.IMAGE,
    "jpeg": ContentType.IMAGE,
    "png": ContentType.IMAGE,
    "webp": ContentType.IMAGE,
    "gif": ContentType.IMAGE,
    "mp4": ContentType.VIDEO,
    "3gp": ContentType.VIDEO,
    "opus": ContentType.AUDIO,
    "ogg": ContentType.AUDIO,
    "m4a": ContentType.AUDIO,
    "mp3": ContentType.AUDIO,
    "pdf": ContentType.DOCUMENT,
    "doc": ContentType.DOCUMENT,
    "docx": ContentType.DOCUMENT,
    "xls": ContentType.DOCUMENT,
    "xlsx": ContentType.DOCUMENT,
    "txt": ContentType.DOCUMENT,
}


@dataclass
class ParseStats:
    """Counters and range information about an export parse."""

    lines: int = 0
    messages: int = 0
    text_messages: int = 0
    media_messages: int = 0
    system_messages: int = 0
    parse_failures: int = 0
    senders: set[str] = field(default_factory=set)
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None


@dataclass
class ParseResult:
    """The normalized messages and the stats of a parse."""

    messages: list[NormalizedMessage]
    stats: ParseStats


@dataclass
class _Header:
    timestamp: datetime
    author: str | None
    body: str


@dataclass
class _Pending:
    header: _Header
    continuation: list[str] = field(default_factory=list)


def _parse_timestamp(date: str, time: str) -> datetime | None:
    cleaned = time.replace(".", "").replace("\u202f", " ").strip()
    patterns = (
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
        "%d/%m/%y %I:%M:%S %p",
        "%d/%m/%y %I:%M %p",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y %I:%M:%S %p",
        "%d/%m/%Y %I:%M %p",
    )
    for pattern in patterns:
        try:
            return datetime.strptime(f"{date} {cleaned}", pattern)
        except ValueError:
            continue
    return None


def _parse_header(line: str) -> _Header | None:
    match = _ANDROID_HEADER.match(line) or _IOS_HEADER.match(line)
    if match is None:
        return None
    date, time, rest = match.groups()
    timestamp = _parse_timestamp(date, time)
    if timestamp is None:
        return None
    if rest is None:
        return _Header(timestamp=timestamp, author=None, body="")
    author, separator, body = rest.partition(": ")
    if separator:
        return _Header(timestamp=timestamp, author=author, body=body)
    return _Header(timestamp=timestamp, author=None, body=rest)


def _kind_from_text(text: str) -> ContentType:
    lowered = text.lower()
    for word, kind in _KIND_WORDS:
        if word in lowered:
            return kind
    return ContentType.UNKNOWN


def _kind_from_name(name: str) -> ContentType:
    extension = name.rsplit(".", 1)[-1].lower()
    return _EXTENSION_KINDS.get(extension, ContentType.UNKNOWN)


def _detect_media(body: str) -> tuple[ContentType | None, str | None, str]:
    """Return (kind, file_name, remaining_text) for a message body."""
    file_match = _MEDIA_FILE_NAME.search(body)
    if file_match is not None:
        name = file_match.group(0)
        remaining = (body[: file_match.start()] + body[file_match.end() :]).strip()
        return _kind_from_name(name), name, remaining
    placeholder = _MEDIA_PLACEHOLDER.search(body)
    if placeholder is not None:
        remaining = (body[: placeholder.start()] + body[placeholder.end() :]).strip()
        return _kind_from_text(placeholder.group(0)), None, remaining
    return None, None, body


def _hash_sender(name: str) -> str:
    return hashlib.sha256(name.strip().casefold().encode("utf-8")).hexdigest()[:16]


def _content_id(timestamp: datetime, author: str, body: str) -> str:
    payload = f"{timestamp.isoformat()}|{author}|{body}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _to_message(
    pending: _Pending, conversation_id: str, stats: ParseStats
) -> NormalizedMessage:
    header = pending.header
    author = header.author or ""
    body = "\n".join([header.body, *pending.continuation]).strip()
    kind, file_name, remaining = _detect_media(body)
    attachments: list[AttachmentRef] = []
    if kind is None:
        content_type = ContentType.TEXT
        text: str | None = body
        stats.text_messages += 1
    else:
        content_type = kind
        text = remaining or None
        stats.media_messages += 1
        attachments.append(AttachmentRef(kind=kind.value, file_name=file_name))
    sender_hash = _hash_sender(author)
    stats.senders.add(sender_hash)
    stats.first_timestamp = (
        header.timestamp
        if stats.first_timestamp is None
        else min(stats.first_timestamp, header.timestamp)
    )
    stats.last_timestamp = (
        header.timestamp
        if stats.last_timestamp is None
        else max(stats.last_timestamp, header.timestamp)
    )
    message_id = _content_id(header.timestamp, author, body)
    return NormalizedMessage(
        id=message_id,
        source_type="whatsapp",
        conversation_id=conversation_id,
        sender_is_admin=False,
        timestamp=header.timestamp,
        content_type=content_type,
        source_message_id=message_id,
        sender_id=sender_hash,
        text=text,
        attachments=attachments,
    )


def parse_export(text: str, *, conversation_id: str = "whatsapp") -> ParseResult:
    """Parse a WhatsApp text export into normalized messages.

    Args:
        text: The raw text of the exported chat.
        conversation_id: Identifier to attach to every produced message.

    Returns:
        The parsed messages and their stats. System messages are counted but not
        emitted; non-header lines before the first message are treated as preamble.
    """
    stats = ParseStats()
    messages: list[NormalizedMessage] = []
    pending: _Pending | None = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        if pending.header.author is None:
            stats.system_messages += 1
        else:
            messages.append(_to_message(pending, conversation_id, stats))
            stats.messages += 1
        pending = None

    for line in text.splitlines():
        stats.lines += 1
        header = _parse_header(line)
        if header is not None:
            flush()
            pending = _Pending(header=header)
        elif pending is not None:
            pending.continuation.append(line)
        elif _LOOKS_LIKE_HEADER.match(line):
            stats.parse_failures += 1
    flush()
    return ParseResult(messages=messages, stats=stats)
