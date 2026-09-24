# SPDX-License-Identifier: MIT
"""Canonical global and logical-space scope keys."""

import re
import uuid
from dataclasses import dataclass

GLOBAL_SCOPE = "global"
_SPACE_ID = re.compile(r"sp_[0-9a-f]{32}\Z")

Scope = str


@dataclass(frozen=True, slots=True)
class ParsedScope:
    """A validated scope and its optional logical space id."""

    kind: str
    space_id: str | None = None


def new_space_id() -> str:
    """Return a new opaque logical space id."""
    return f"sp_{uuid.uuid4().hex}"


def is_space_id(space_id: str) -> bool:
    """Return whether a value is a canonical logical space id."""
    return _SPACE_ID.fullmatch(space_id) is not None


def scope_for_space(space_id: str) -> str:
    """Return the canonical scope key for a logical space."""
    if not is_space_id(space_id):
        message = f"invalid space id: {space_id!r}"
        raise ValueError(message)
    return f"space:{space_id}"


def parse_scope(value: str) -> ParsedScope:
    """Parse a canonical global or logical-space scope key."""
    if value == GLOBAL_SCOPE:
        return ParsedScope(kind="global")
    if value.startswith("space:"):
        space_id = value.removeprefix("space:")
        if is_space_id(space_id):
            return ParsedScope(kind="space", space_id=space_id)
    message = f"invalid scope key: {value!r}"
    raise ValueError(message)


def is_global(scope: Scope) -> bool:
    """Return whether a scope is the shared global layer."""
    return scope == GLOBAL_SCOPE
