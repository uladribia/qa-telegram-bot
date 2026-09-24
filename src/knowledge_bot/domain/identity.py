# SPDX-License-Identifier: MIT
"""Generic identity helpers for source instances and principals."""

import hashlib


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
