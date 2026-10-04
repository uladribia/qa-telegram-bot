# SPDX-License-Identifier: MIT
"""Cloudflare Worker entrypoint for fetch requests and scheduled reports."""

import logging
import os

# Pydantic imports every installed ``pydantic`` entry point. The Workers
# runtime forbids entropy while a Worker is starting, and some plugins need
# one, so the Worker would fail to boot without this. Instrumented Pydantic
# models are not used here. This must run before the first pydantic import
# below.
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "true")

import time
from collections.abc import Awaitable
from typing import cast

from fastapi import Request
from pyodide.ffi import create_proxy
from workers import Request as WorkerRequest
from workers import WorkerEntrypoint, asgi, wait_until
from workers.asgi import run_in_background

from knowledge_bot.api.app import create_app
from knowledge_bot.infrastructure.composition import (
    WorkerEnv,
    build_context,
)
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.infrastructure.logging import (
    configure_logging,
    configure_observability,
)

_context: AppContext | None = None
_scheduled_context: AppContext | None = None


def _resolve_context(request: Request) -> AppContext:
    """Build the context once per isolate from request bindings."""
    global _context
    if _context is None:
        env = cast("WorkerEnv", request.scope["env"])
        _context = build_context(env)
        configure_observability(capture_content=_context.settings.capture_content)
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
        log = logging.getLogger("knowledge_bot.scheduled")
        fields = {"use_case": "scheduled_daily_report", "trigger": "cron"}
        log.info("scheduled_started", extra=fields)
        try:
            if _scheduled_context is None:
                scheduled_env = cast("WorkerEnv", env)
                _scheduled_context = build_context(scheduled_env)
                configure_observability(
                    capture_content=_scheduled_context.settings.capture_content
                )
            sent = await _scheduled_context.daily_report.run()
        except Exception:
            log.exception(
                "scheduled_failed",
                extra={
                    **fields,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            raise
        log.info(
            "scheduled_finished",
            extra={
                **fields,
                "sent": sent,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
