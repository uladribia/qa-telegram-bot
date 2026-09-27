# SPDX-License-Identifier: MIT
"""FastAPI application factory.

This module is composition only: build the app, register the canonical API
routes, register the enabled connector routes, return. It holds no business
logic and no channel behaviour, so the canonical API and every adapter reach
the same application services through the same context.
"""

from fastapi import FastAPI

from knowledge_bot.adapters.telegram.routes import Deferrer, register_telegram_routes
from knowledge_bot.api.context import ContextResolver
from knowledge_bot.api.routes.internal import build_internal_router
from knowledge_bot.api.routes.system import build_system_router
from knowledge_bot.api.routes.v1 import build_api_router


def create_app(
    resolve_context: ContextResolver, defer: Deferrer | None = None
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        resolve_context: Returns the application context for a request.
        defer: Hands Telegram update processing to the platform after the
            response, so the webhook is acknowledged before the AI pipeline
            runs. The Worker passes one; the local runtime processes inline.

    Returns:
        The configured FastAPI app.
    """
    app = FastAPI(title="knowledge-bot")
    app.include_router(build_system_router())
    app.include_router(build_api_router(resolve_context))
    app.include_router(build_internal_router(resolve_context))
    register_telegram_routes(app, resolve_context, defer)
    return app
