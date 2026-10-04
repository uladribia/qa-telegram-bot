# SPDX-License-Identifier: MIT
"""Central logging configuration.

There is one logging system: the standard library. Application events are
emitted with contextual fields and printed to stderr, structured JSON in the
Worker and human-readable locally. Spans are one line each, written by
``infrastructure/telemetry.py``; no tracing SDK is imported anywhere, so the
same code path serves the Worker, the local runtime, and tests.

Reading a flow back is a matter of grepping ``request_id`` in
``wrangler tail`` or ``docker logs``. Nothing is exported off the Worker.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from logging import getLogger
from time import perf_counter

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from knowledge_bot.infrastructure.telemetry import (
    LogTracer,
    bind_request_id,
    new_request_id,
)
from knowledge_bot.ports.telemetry import Tracer

#: Value shapes that must never leave the process, whatever key holds them.
#: A Telegram bot token is a *value* shaped like ``<digits>:<base64url>`` and
#: the transport puts it in the request URL, where no key name reveals it.
#: The lookarounds are deliberate: ``\b`` would not work here, because the
#: token in a URL is preceded by ``bot`` and the character before the digits
#: is a word character, so a word boundary never falls where the digits start.
SECRET_VALUE_PATTERNS = (r"(?<!\d)\d{8,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])",)

#: The same patterns compiled once, for scrubbing every rendered field.
_SECRET_VALUES = re.compile("|".join(SECRET_VALUE_PATTERNS))

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


def configure_observability(*, capture_content: bool = False) -> None:
    """Turn on content capture for this process.

    There is no exporter to configure: the standard library sink installed by
    ``configure_logging`` already receives every event. This only records the
    capture mode, which decides whether message text, prompts, and answers are
    written at all. Credential scrubbing is not a mode and is always on.

    Args:
        capture_content: Record message text, sender identity, prompts, and
            answers. On while testing, so a flow can be reconstructed; the
            content itself comes from ``log_content`` at the flow boundaries.
    """
    global _capture_content
    _capture_content = capture_content


class RequestLogger:
    """One log line per HTTP request, and the correlation id for its spans.

    This replaces what automatic instrumentation used to provide: a line per
    request carrying the outcome, plus a ``request_id`` that every span opened
    while serving it inherits. That is the whole reconstruction story — grep
    the id, read the legs in order.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Store the wrapped application.

        Args:
            app: The next ASGI application in the chain.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one request, logging its method, path, status, and duration.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        identifier = new_request_id()
        started = perf_counter()
        status = 500
        with bind_request_id(identifier):

            async def capture_status(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                await send(message)

            try:
                await self.app(scope, receive, capture_status)
            except Exception:
                getLogger("knowledge_bot.request").exception(
                    "http_request_failed",
                    extra={
                        "request_id": identifier,
                        "method": scope.get("method", "-"),
                        "path": scope.get("path", "-"),
                        "duration_ms": round((perf_counter() - started) * 1000, 2),
                    },
                )
                raise
        getLogger("knowledge_bot.request").info(
            "http_request",
            extra={
                "request_id": identifier,
                "method": scope.get("method", "-"),
                "path": scope.get("path", "-"),
                "status": status,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )


def log_content(event: str, **fields: object) -> None:
    """Record one content-carrying event on the process log.

    The event is dropped when content capture is off. Values are scrubbed by
    shape on the way out, so a credential can never reach the log whatever key
    holds it.

    Args:
        event: The event name, in the existing ``snake_case`` style.
        **fields: The content and context to attach to the event.
    """
    if not _capture_content:
        return
    getLogger("knowledge_bot.content").info(event, extra=fields)


def content_capture_enabled() -> bool:
    """Return whether content capture is on for this process."""
    return _capture_content


def build_tracer() -> Tracer:
    """Return the tracer for this process.

    The application layer asks for spans without knowing how they are recorded.
    The same tracer serves every runtime, because recording a span is a
    ``logging`` call and needs no exporter, no token, and no configuration.
    """
    return LogTracer(capture=_capture_content)


def _fields(record: logging.LogRecord) -> dict[str, object]:
    """Return the contextual fields a caller attached to one record.

    Every value is scrubbed by shape before it can be rendered, which is what
    keeps a bot token out of the log when it rides inside a URL.
    """
    return {
        name: _scrub(value)
        for name, value in record.__dict__.items()
        if name not in _RESERVED_RECORD_FIELDS and not name.startswith("_")
    }


def _scrub(value: object) -> object:
    """Replace a credential-shaped value with a marker.

    Args:
        value: The field value as recorded.

    Returns:
        The value with any credential-shaped text replaced, or a ``list`` or
        ``dict`` rebuilt with the same treatment applied inside it.
    """
    if isinstance(value, str):
        return _SECRET_VALUES.sub("[scrubbed]", value)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


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
            # An HTTP error carries the request URL, which is where a Telegram
            # bot token lives, so the traceback is scrubbed like any field.
            payload["exception"] = _SECRET_VALUES.sub(
                "[scrubbed]", self.formatException(record.exc_info)
            )
        return json.dumps(payload, default=str, separators=(",", ":"))
