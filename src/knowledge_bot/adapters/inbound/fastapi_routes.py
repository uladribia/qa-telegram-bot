# SPDX-License-Identifier: MIT
"""FastAPI application factory and HTTP routes.

The Worker bindings are only available per request (in ``request.scope["env"]``),
so the app resolves its context through a callable rather than at import time.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request

from knowledge_bot.adapters.inbound.telegram import (
    is_valid_webhook_secret,
    normalize_message,
)
from knowledge_bot.application.intake import IntakeAction, decide_intake
from knowledge_bot.contracts.telegram import TelegramUpdate
from knowledge_bot.infrastructure.composition import AppContext
from knowledge_bot.infrastructure.logging import configure_logging
from knowledge_bot.infrastructure.security import secrets_match

ContextResolver = Callable[[Request], AppContext]


def create_app(resolve_context: ContextResolver) -> FastAPI:
    """Build the FastAPI application.

    Args:
        resolve_context: Returns the application context for a request.

    Returns:
        The configured FastAPI app.
    """
    configure_logging()
    app = FastAPI(title="knowledge-bot")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Report Worker liveness."""
        return {"status": "ok"}

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        secret: Annotated[
            str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")
        ] = None,
    ) -> dict[str, str]:
        """Receive Telegram updates, ingest them, and answer when addressed."""
        context = resolve_context(request)
        if not is_valid_webhook_secret(
            secret, context.settings.telegram_webhook_secret
        ):
            raise HTTPException(status_code=401, detail="invalid secret")
        update = TelegramUpdate.model_validate(await request.json())
        message = normalize_message(update, context.identity)
        if message is None:
            return {"status": "ignored"}
        action = decide_intake(
            message,
            background_listener_enabled=context.settings.background_listener_enabled,
        )
        if action is IntakeAction.IGNORE:
            return {"status": "ignored"}
        await context.ingestor.ingest(message)
        await context.recap.maybe_send(message.conversation_id)
        return {"status": action.value}

    @app.post("/internal/recap")
    async def internal_recap(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Force the opportunistic recap check from an external scheduler."""
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        sent = await context.recap.maybe_send(context.settings.allowed_telegram_chat_id)
        return {"status": "sent" if sent else "skipped"}

    return app
