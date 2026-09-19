# SPDX-License-Identifier: MIT
"""FastAPI application factory and HTTP routes."""

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

configure_logging()


def create_app(context: AppContext) -> FastAPI:
    """Build the FastAPI application.

    Args:
        context: The wired application context.

    Returns:
        The configured FastAPI app.
    """
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
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Force the opportunistic recap check from an external scheduler."""
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        sent = await context.recap.maybe_send(context.settings.allowed_telegram_chat_id)
        return {"status": "sent" if sent else "skipped"}

    return app
