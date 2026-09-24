# SPDX-License-Identifier: MIT
"""Telegram webhook HTTP adapter."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request

from knowledge_bot.adapters.inbound.telegram import (
    is_valid_webhook_secret,
)
from knowledge_bot.contracts.telegram import TelegramUpdate
from knowledge_bot.infrastructure.composition import AppContext

TelegramUpdateHandler = Callable[[AppContext, TelegramUpdate], Awaitable[str]]


def register_telegram_routes(
    app: FastAPI,
    resolve_context: Callable[[Request], AppContext],
    handle_update: TelegramUpdateHandler,
) -> None:
    """Register the authenticated Telegram webhook on the HTTP app."""

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        secret: Annotated[
            str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")
        ] = None,
    ) -> dict[str, str]:
        """Validate and normalize one Telegram update."""
        context = resolve_context(request)
        if not is_valid_webhook_secret(
            secret, context.settings.telegram_webhook_secret
        ):
            raise HTTPException(status_code=401, detail="invalid secret")
        update = TelegramUpdate.model_validate(await request.json())
        return {"status": await handle_update(context, update)}
