# SPDX-License-Identifier: MIT
"""Telegram webhook HTTP adapter."""

from collections.abc import Awaitable, Callable
from typing import Annotated, cast

from fastapi import FastAPI, Header, HTTPException, Request
from loguru import logger

from knowledge_bot.adapters.inbound.telegram import (
    is_valid_webhook_secret,
)
from knowledge_bot.contracts.telegram import TelegramUpdate
from knowledge_bot.infrastructure.context import AppContext

TelegramUpdateHandler = Callable[[AppContext, TelegramUpdate], Awaitable[str]]
Deferrer = Callable[[Awaitable[str]], None]


def register_telegram_routes(
    app: FastAPI,
    resolve_context: Callable[[Request], AppContext | Awaitable[AppContext]],
    handle_update: TelegramUpdateHandler,
    defer: Deferrer | None = None,
) -> None:
    """Register the authenticated Telegram webhook on the HTTP app.

    Args:
        app: The application to register the route on.
        resolve_context: Returns the application context for a request.
        handle_update: Processes one validated Telegram update.
        defer: Hands the processing to the platform after the response, as the
            Worker does so a slow model cannot outlast Telegram's read timeout.
            Without it the update is processed inline, as the local runtime does.
    """

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        secret: Annotated[
            str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")
        ] = None,
    ) -> dict[str, str]:
        """Validate, acknowledge, and process one Telegram update."""
        context = resolve_context(request)
        if isinstance(context, Awaitable):
            context = await cast(Awaitable[AppContext], context)
        else:
            context = cast(AppContext, context)
        if not is_valid_webhook_secret(
            secret, context.settings.telegram_webhook_secret
        ):
            raise HTTPException(status_code=401, detail="invalid secret")
        update = TelegramUpdate.model_validate(await request.json())
        processing = handle_update(context, update)
        if defer is None:
            return {"status": await processing}
        logger.bind(use_case="telegram_webhook", update_id=update.update_id).info(
            "telegram_webhook_accepted"
        )
        defer(processing)
        return {"status": "accepted"}
