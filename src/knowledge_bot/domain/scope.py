# SPDX-License-Identifier: MIT
"""Knowledge scope: global knowledge vs per-group knowledge.

A scope is either the literal ``"global"`` (knowledge every group can see) or
the external id of the conversation (Telegram chat id) the knowledge belongs
to. Messages are always scoped by their own conversation; the scope tag only
applies to sources and Q&A items.
"""

GLOBAL_SCOPE = "global"

#: ``"global"`` or a conversation external id (e.g. a Telegram chat id).
Scope = str


def is_global(scope: Scope) -> bool:
    """Return whether a scope is the shared global layer.

    Args:
        scope: The scope value.

    Returns:
        ``True`` for the global scope.
    """
    return scope == GLOBAL_SCOPE
