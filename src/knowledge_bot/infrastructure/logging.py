# SPDX-License-Identifier: MIT
"""Central logging and telemetry configuration.

There is one logging system: the standard library. Application events are
emitted with contextual fields, printed to stderr (structured JSON in the
Worker, human-readable locally), and forwarded to Logfire as spans. Loguru is
gone; this module is the only place handlers and the exporter are configured.
"""

import json
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from logging import Logger, getLogger
from types import ModuleType

from fastapi import FastAPI

from knowledge_bot.infrastructure.telemetry import LogfireTracer, _as_sdk
from knowledge_bot.ports.telemetry import NoopTracer, Tracer

#: Service identity in Logfire. One deployable means one service name.
SERVICE_NAME = "qa-telegram"

#: Value shapes that must never leave the process, whatever key holds them.
#: The SDK already scrubs by key name (``secret``, ``password``, ``token``, the
#: webhook secret header), but a Telegram bot token is a *value* shaped like
#: ``<digits>:<base64url>`` and the transport puts it in the request URL, where
#: no key name reveals it.
SECRET_VALUE_PATTERNS = (r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b",)

#: Attributes every ``LogRecord`` carries; everything else came from the caller.
_RESERVED_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_configured = False
_observability_configured = False
_capture_content = False


def configure_logging(*, json_logs: bool = False) -> None:
    """Configure the process-wide stderr sink for the standard library.

    Args:
        json_logs: Emit structured JSON for the Worker instead of human text.
    """
    global _configured
    if _configured:
        return
    root = getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if json_logs else _TextFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO if json_logs else logging.DEBUG)
    _configured = True


def configure_observability(
    app: FastAPI,
    *,
    environment: str,
    token: str | None = None,
    send_to_logfire: bool = True,
    instrument_client: bool = False,
    capture_content: bool = False,
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
        capture_content: Export message text, sender identity, prompts, and
            answers. On while testing, so a flow can be reconstructed; the
            scrubbing that hides credentials is unaffected and always on. The
            content itself comes from ``log_content`` at the flow boundaries.
    """
    global _observability_configured, _capture_content
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
            scrubbing=logfire.ScrubbingOptions(
                extra_patterns=list(SECRET_VALUE_PATTERNS)
            ),
        ),
    )
    _isolated(
        "fastapi",
        lambda: logfire.instrument_fastapi(app, capture_headers=capture_content),
    )
    if instrument_client:
        _isolated(
            "httpx",
            lambda: logfire.instrument_httpx(
                capture_request_body=capture_content,
                capture_response_body=capture_content,
            ),
        )
    _bridge_standard_library_logging(logfire)
    _capture_content = capture_content
    _observability_configured = True


def log_content(event: str, **fields: object) -> None:
    """Record one content-carrying event in Logfire.

    The event is dropped when content capture is off, when observability was
    never configured (unit tests), or when the SDK import failed. Fields use
    structured placeholders, so they stay searchable and are scrubbed by key
    name and by value shape.

    Args:
        event: The event name, in the existing ``snake_case`` style.
        **fields: The content and context to attach to the event.
    """
    if not _capture_content or not _observability_configured:
        return
    _isolated(event, lambda: _logfire().info(event, **fields))


def content_capture_enabled() -> bool:
    """Return whether content capture is on for this process."""
    return _capture_content and _observability_configured


def build_tracer() -> Tracer:
    """Return the tracer for this process.

    The application layer asks for spans without knowing whether telemetry was
    configured, so a runtime that never configured the exporter still gets a
    working tracer: the no-op one, which records nothing and cannot fail.
    """
    if not _observability_configured:
        return NoopTracer()
    return LogfireTracer(sdk=_as_sdk(_logfire()), capture=_capture_content)


def _logfire() -> ModuleType:
    """Import the SDK lazily.

    The Cloudflare Python runtime forbids entropy calls while a Worker is
    starting, and importing the OpenTelemetry SDK needs one. Importing here
    means it happens on the first request, never at module import time.
    """
    import logfire

    return logfire


def _isolated(step: str, action: Callable[[], None]) -> None:
    """Run one telemetry step, logging and swallowing any failure.

    Args:
        step: Name of the step, recorded in the warning.
        action: The step itself.
    """
    try:
        action()
    except Exception as error:
        getLogger("knowledge_bot.telemetry").warning(
            "logfire_step_failed", extra={"step": step, "error": type(error).__name__}
        )


def _bridge_standard_library_logging(logfire: ModuleType) -> None:
    """Send standard-library records to Logfire without changing levels.

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


def _fields(record: logging.LogRecord) -> dict[str, object]:
    """Return the contextual fields a caller attached to one record."""
    return {
        name: value
        for name, value in record.__dict__.items()
        if name not in _RESERVED_RECORD_FIELDS and not name.startswith("_")
    }


class _TextFormatter(logging.Formatter):
    """Render a record as ``LEVEL message key=value`` for local development."""

    def format(self, record: logging.LogRecord) -> str:
        """Format one record.

        Args:
            record: The record to render.

        Returns:
            The human-readable line.
        """
        fields = " ".join(f"{k}={v}" for k, v in sorted(_fields(record).items()))
        base = f"{record.levelname} {record.name} {record.getMessage()}"
        return f"{base} {fields}".rstrip()


class _JsonFormatter(logging.Formatter):
    """Render a record as one structured JSON object for the Worker."""

    def format(self, record: logging.LogRecord) -> str:
        """Format one record.

        Args:
            record: The record to render.

        Returns:
            The JSON line, with the exception text when the call failed.
        """
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))
