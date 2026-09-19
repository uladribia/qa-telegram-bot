# SPDX-License-Identifier: MIT
"""Cloudflare Worker entrypoint for the knowledge bot."""

from typing import cast

from fastapi import Request
from workers import asgi

from knowledge_bot.adapters.inbound.fastapi_routes import create_app
from knowledge_bot.infrastructure.composition import (
    AppContext,
    WorkerEnv,
    build_context,
)

_context: AppContext | None = None


def _resolve_context(request: Request) -> AppContext:
    """Build the context once per isolate from the request bindings."""
    global _context
    if _context is None:
        env = cast("WorkerEnv", request.scope["env"])
        _context = build_context(env)
    return _context


app = create_app(_resolve_context)

Default = asgi.entrypoint(app)
