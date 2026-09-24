# SPDX-License-Identifier: MIT
"""Generic identity helpers for source instances and principals."""

import hashlib
import unicodedata


def normalize_canonical_question(question: str) -> str:
    """Normalize a question for semantic canonical identity."""
    normalized = unicodedata.normalize("NFKC", question).casefold()
    return " ".join(normalized.split())


def canonical_key_for(question: str) -> str:
    """Return the semantic canonical key for a question."""
    normalized = normalize_canonical_question(question)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def source_instance_id(kind: str, *parts: str) -> str:
    """Return a deterministic source-instance id for a connector-declared kind."""
    if not kind.strip() or any(not part for part in parts):
        message = "source kind and identity parts are required"
        raise ValueError(message)
    value = "\n".join(parts)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"src:{kind}:{digest}"


def principal_id(channel: str, external_user_id: str) -> str:
    """Return an opaque principal id for a channel user."""
    if not channel.strip() or not external_user_id.strip():
        message = "channel and external user id are required"
        raise ValueError(message)
    return f"{channel}:{external_user_id}"


def split_principal_id(value: str) -> tuple[str, str]:
    """Split and validate an opaque channel principal id."""
    channel, separator, external_id = value.partition(":")
    if not separator or not channel or not external_id or ":" in external_id:
        message = "principal id must have the form <channel>:<external-id>"
        raise ValueError(message)
    return channel, external_id
