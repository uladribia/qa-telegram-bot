# SPDX-License-Identifier: MIT
"""Cloudflare Worker entrypoint for fetch requests and scheduled reports."""

import time
from collections.abc import Awaitable
from typing import cast

from fastapi import Request
from loguru import logger
from pyodide.ffi import create_proxy
from workers import Request as WorkerRequest
from workers import WorkerEntrypoint, asgi, wait_until
from workers.asgi import run_in_background

from knowledge_bot.adapters.http.app import create_app
from knowledge_bot.infrastructure.composition import WorkerEnv, build_context
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.infrastructure.logging import configure_logging

_context: AppContext | None = None
_scheduled_context: AppContext | None = None


def _resolve_context(request: Request) -> AppContext:
    """Build the context once per isolate from request bindings."""
    global _context
    if _context is None:
        _context = build_context(cast("WorkerEnv", request.scope["env"]))
    return _context


def _defer(processing: Awaitable[str]) -> None:
    """Keep one Telegram update alive after the webhook has been acknowledged.

    Telegram drops a webhook request whose response arrives late and retries
    the update, so the AI pipeline must not sit between the request and the
    response. The background task logs its own failures through
    ``telegram_webhook_failed``.
    """
    wait_until(create_proxy(run_in_background(processing)))


configure_logging(json_logs=True)
app = create_app(_resolve_context, _defer)


class Default(WorkerEntrypoint):
    """Serve HTTP requests and the configured daily report schedule."""

    async def fetch(self, request: WorkerRequest) -> object:
        """Serve one ASGI request with Worker bindings."""
        return await asgi.fetch(app, request, self.env, self.ctx)

    async def scheduled(self, controller: object, env: object, ctx: object) -> None:
        """Run the deterministic daily report for the configured Cron Trigger.

        A scheduled handler that raises leaves no trace in the request logs, so
        the outcome is logged explicitly and the failure is re-raised for the
        platform to record.
        """
        del controller, ctx
        global _scheduled_context
        started = time.perf_counter()
        logger.bind(use_case="scheduled_daily_report", trigger="cron").info(
            "scheduled_started"
        )
        try:
            if _scheduled_context is None:
                _scheduled_context = build_context(cast("WorkerEnv", env))
            sent = await _scheduled_context.daily_report.run()
        except Exception:
            logger.bind(
                use_case="scheduled_daily_report",
                trigger="cron",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            ).exception("scheduled_failed")
            raise
        logger.bind(
            use_case="scheduled_daily_report",
            trigger="cron",
            sent=sent,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        ).info("scheduled_finished")
