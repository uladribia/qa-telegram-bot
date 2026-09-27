# SPDX-License-Identifier: MIT
"""Central logging configuration: the only place sinks and telemetry are set up.

Loguru remains the application's own logger; Logfire exports traces built from
the request surface. Message text, answers, prompts, and sender identity are
never exported: only request metadata, durations, counts, and model names.
"""

import sys
from collections.abc import Callable
from logging import Logger, getLogger
from types import ModuleType

from fastapi import FastAPI
from loguru import logger

#: Service identity in Logfire. One deployable means one service name.
SERVICE_NAME = "qa-telegram"

_configured = False
_observability_configured = False


def configure_logging(*, json_logs: bool = False) -> None:
    """Configure the process-wide Loguru sink.

    Args:
        json_logs: Emit structured JSON (production) instead of human text
            (development).
    """
    global _configured
    if _configured:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO" if json_logs else "DEBUG",
        serialize=json_logs,
        backtrace=False,
        diagnose=False,
    )
    _configured = True


def configure_observability(
    app: FastAPI,
    *,
    environment: str,
    token: str | None = None,
    send_to_logfire: bool = True,
    instrument_client: bool = False,
) -> None:
    """Configure Logfire once per process and instrument the HTTP surface.

    A runtime without a write token — a fresh checkout, a container, CI — still
    works: the exporter stays local instead of failing the process. Telemetry
    is never allowed to break the answer path, so every step is isolated: a
    failed instrumentor is logged and skipped, not raised.

    Args:
        app: The FastAPI application whose requests become spans.
        environment: Logfire environment label, ``local`` or ``cloudflare``.
        token: Send-only write token. ``None`` means "use ``LOGFIRE_TOKEN`` or
            the local project credentials if present, otherwise stay local".
        send_to_logfire: Master switch. Tests set it to ``False`` so a
            developer's project credentials cannot receive test traffic.
        instrument_client: Also instrument the outbound HTTP client. Only the
            local runtime has one: the Worker calls Cloudflare bindings, and
            the SDK raises when the client library is absent from the bundle.
    """
    global _observability_configured
    if _observability_configured:
        return
    logfire = _logfire()
    _isolated(
        "configure",
        lambda: logfire.configure(
            service_name=SERVICE_NAME,
            environment=environment,
            token=token,
            send_to_logfire=send_to_logfire and "if-token-present",
            console=False,
        ),
    )
    _isolated("fastapi", lambda: logfire.instrument_fastapi(app))
    if instrument_client:
        _isolated("httpx", logfire.instrument_httpx)
    _bridge_standard_library_logging(logfire)
    _observability_configured = True


def _isolated(step: str, action: Callable[[], None]) -> None:
    """Run one telemetry step, logging and swallowing any failure.

    Args:
        step: Name of the step, recorded in the warning.
        action: The step itself.
    """
    try:
        action()
    except Exception as error:
        logger.bind(
            use_case="configure_observability",
            step=step,
            error=type(error).__name__,
        ).warning("logfire_step_failed")


def _logfire() -> ModuleType:
    """Import the SDK lazily.

    The Cloudflare Python runtime forbids entropy calls while a Worker is
    starting, and importing the OpenTelemetry SDK needs one. Importing here
    means it happens on the first request, never at module import time.
    """
    import logfire

    return logfire


def _bridge_standard_library_logging(logfire: ModuleType) -> None:
    """Forward standard-library records to Logfire without changing levels.

    Uvicorn and the ASGI stack log through the standard library. Adding the
    handler here leaves the existing console output and the configured
    thresholds untouched; only records that already pass their logger's level
    are exported.
    """
    root: Logger = getLogger()
    if not any(
        isinstance(handler, logfire.LogfireLoggingHandler) for handler in root.handlers
    ):
        root.addHandler(logfire.LogfireLoggingHandler())
