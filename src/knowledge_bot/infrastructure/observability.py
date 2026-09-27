# SPDX-License-Identifier: MIT
"""Logfire configuration: the only place observability is set up.

Loguru stays the application's own logger (see
``knowledge_bot.infrastructure.logging``); this module exports request,
spans, and standard-library records to Logfire without adding a second sink to
the application logger. Message text, answers, and sender identity never reach
telemetry: only request metadata, durations, and model names are captured.
"""

import logging

import logfire
from fastapi import FastAPI
from logfire.exceptions import LogfireConfigError
from loguru import logger

from knowledge_bot.infrastructure.settings import Settings

_configured = False


def configure_observability(app: FastAPI, settings: Settings) -> None:
    """Configure Logfire once per process and instrument the HTTP surface.

    A runtime without a write token (a container, CI, a developer machine that
    never ran ``logfire init``) must still start: the exporter is downgraded to
    a local no-op instead of failing the process.

    Args:
        app: The FastAPI application whose requests become spans.
        settings: Runtime settings, including the service name, the
            environment label, and the telemetry kill switch.
    """
    global _configured
    if _configured:
        return
    _configure_exporter(settings)
    logfire.instrument_fastapi(app)
    logfire.instrument_httpx()
    _bridge_standard_library_logging()
    _configured = True


def _configure_exporter(settings: Settings) -> None:
    """Send spans to Logfire when a write token is available.

    Args:
        settings: Runtime settings naming the service, the environment, and
            whether telemetry may leave the process.
    """
    service_name = settings.logfire_service_name
    environment = settings.logfire_environment or settings.runtime.value
    try:
        logfire.configure(
            service_name=service_name,
            environment=environment,
            send_to_logfire=settings.logfire_send_to_logfire,
            console=False,
        )
    except LogfireConfigError:
        logfire.configure(
            service_name=service_name,
            environment=environment,
            send_to_logfire=False,
            console=False,
        )
        logger.bind(
            use_case="configure_observability",
            service_name=service_name,
        ).warning(
            "logfire_send_unavailable: no write token found, spans stay local. "
            "Run `logfire init use --name qa-telegram --permission send` or set "
            "LOGFIRE_TOKEN."
        )


def _bridge_standard_library_logging() -> None:
    """Forward standard-library records to Logfire without changing levels.

    Uvicorn and the ASGI stack log through the standard library. Adding the
    handler here leaves the existing console output and the configured
    thresholds untouched; only records that already pass their logger's level
    are exported.
    """
    root_logger = logging.getLogger()
    if not any(
        isinstance(handler, logfire.LogfireLoggingHandler)
        for handler in root_logger.handlers
    ):
        root_logger.addHandler(logfire.LogfireLoggingHandler())
