# SPDX-License-Identifier: MIT
"""Request-scoped context resolution and internal authentication.

The Worker bindings are only available per request (in
``request.scope["env"]``), so routes resolve their context through a callable
rather than at import time. Both the canonical API and the enabled adapters go
through here, which is what lets them share one application context.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, cast

from fastapi import Header, HTTPException, Request

from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.infrastructure.security import secrets_match

ContextResolver = Callable[[Request], AppContext | Awaitable[AppContext]]

InternalKey = Annotated[str | None, Header(alias="X-Internal-Key")]


async def resolve_app_context(
    resolve_context: ContextResolver, request: Request
) -> AppContext:
    """Resolve either the Worker or local asynchronous context.

    Args:
        resolve_context: Returns the application context for a request.
        request: The request being served.

    Returns:
        The resolved application context.
    """
    context = resolve_context(request)
    if isinstance(context, Awaitable):
        return await cast(Awaitable[AppContext], context)
    return cast(AppContext, context)


async def internal_context(
    request: Request, key: str | None, resolve_context: ContextResolver
) -> AppContext:
    """Resolve the context for an operator endpoint, refusing a bad key.

    Args:
        request: The request being served.
        key: The supplied ``X-Internal-Key`` header.
        resolve_context: Returns the application context for a request.

    Returns:
        The resolved application context.

    Raises:
        HTTPException: 401 when the key does not match the configured one.
    """
    context = await resolve_app_context(resolve_context, request)
    if not secrets_match(key, context.settings.internal_admin_key):
        raise HTTPException(status_code=401, detail="invalid key")
    return context
