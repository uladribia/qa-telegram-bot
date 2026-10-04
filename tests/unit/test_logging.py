# SPDX-License-Identifier: MIT
"""Unit tests for the single logging configuration and its formatters."""

import json
import logging
import sys
from collections.abc import Iterator
from types import TracebackType

import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from knowledge_bot.infrastructure import logging as logging_config
from knowledge_bot.infrastructure.logging import (
    RequestLogger,
    _JsonFormatter,
    _TextFormatter,
    configure_logging,
    content_capture_enabled,
    log_content,
)
from knowledge_bot.infrastructure.telemetry import LogTracer

type ExcInfo = tuple[type[BaseException], BaseException, TracebackType]


class _HandlerError(RuntimeError):
    """Stands in for a request handler that fails."""

    def __init__(self) -> None:
        """Report the fixed failure this fake always raises."""
        super().__init__("handler exploded")


@pytest.fixture
def fresh_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Let one test configure the process, then restore the global state."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    monkeypatch.setattr(logging_config, "_configured", False)
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="knowledge_bot.webhook",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_json_formatter_keeps_contextual_fields() -> None:
    """The Worker sink emits one JSON object with the caller's fields."""
    record = _record("telegram_webhook_processed")
    record.use_case = "telegram_webhook"
    record.duration_ms = 12.5

    payload = json.loads(_JsonFormatter().format(record))

    assert payload["event"] == "telegram_webhook_processed"
    assert payload["level"] == "INFO"
    assert payload["use_case"] == "telegram_webhook"
    assert payload["duration_ms"] == 12.5
    assert "timestamp" in payload


def test_json_formatter_includes_the_exception() -> None:
    """A failed call carries its traceback into the structured line."""
    record = _record("telegram_webhook_failed", logging.ERROR)
    record.exc_info = _boom()

    payload = json.loads(_JsonFormatter().format(record))

    assert "RuntimeError: boom" in payload["exception"]


def _boom() -> ExcInfo:
    """Return a real exception triple, captured while it is being handled."""
    try:
        error = RuntimeError("boom")
        raise error
    except RuntimeError:
        info = sys.exc_info()
        assert info[0] is not None
        return (info[0], info[1], info[2])


def test_text_formatter_is_human_readable() -> None:
    """The local sink keeps the developer-facing one-line shape."""
    record = _record("telegram_webhook_processed")
    record.use_case = "telegram_webhook"

    line = _TextFormatter().format(record)

    assert line.startswith("INFO knowledge_bot.webhook telegram_webhook_processed")
    assert line.endswith("use_case=telegram_webhook")


def test_configure_logging_writes_to_stderr_only(
    fresh_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Events reach stderr, never stdout, and only one handler is installed."""
    configure_logging(json_logs=False)
    configure_logging(json_logs=True)

    logging.getLogger("knowledge_bot.test").info("configured", extra={"k": "v"})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configured" in captured.err
    assert len(logging.getLogger().handlers) == 1


def test_json_logs_are_the_worker_sink(
    fresh_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The production shape is one JSON object per line."""
    configure_logging(json_logs=True)

    logging.getLogger("knowledge_bot.test").warning(
        "workers_ai_call_failed",
        extra={"operation": "generation", "error": "TimeoutError"},
    )

    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["event"] == "workers_ai_call_failed"
    assert payload["level"] == "WARNING"
    assert payload["operation"] == "generation"
    assert payload["error"] == "TimeoutError"


def test_logging_has_no_file_handler(fresh_logging: None) -> None:
    """The process keeps no log file, as the logging rules require."""
    configure_logging(json_logs=False)

    assert not any(
        isinstance(handler, logging.FileHandler)
        for handler in logging.getLogger().handlers
    )


def test_content_events_are_dropped_when_capture_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A content event is not written when capture is off."""
    monkeypatch.setattr(logging_config, "_capture_content", False)

    assert content_capture_enabled() is False

    log_content("telegram_outbound", text="must not be recorded")


def _http_app(status: int = 200, fail: bool = False) -> ASGIApp:
    """Return a minimal ASGI app for the request logger to wrap."""
    from starlette.types import Receive, Scope, Send

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if fail:
            raise _HandlerError
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


async def _call(app: ASGIApp, path: str = "/adapters/telegram/webhook") -> None:
    """Drive one HTTP request through an ASGI app."""

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        del message

    await app({"type": "http", "method": "POST", "path": path}, receive, send)


@pytest.mark.asyncio
async def test_a_request_is_logged_once_with_its_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The request line is the entry point to reconstructing a flow."""
    caplog.set_level(logging.INFO, logger="knowledge_bot.request")

    await _call(RequestLogger(_http_app(status=202)))

    lines = [record for record in caplog.records if record.message == "http_request"]
    assert len(lines) == 1
    assert lines[0].__dict__["status"] == 202
    assert lines[0].__dict__["method"] == "POST"
    assert lines[0].__dict__["path"] == "/adapters/telegram/webhook"
    assert lines[0].__dict__["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_spans_opened_while_serving_share_the_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A span written during the request carries the request's id."""
    caplog.set_level(logging.INFO)

    async def app_with_a_span(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        with LogTracer(capture=False).span("answer_question", reason="answered"):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    await _call(RequestLogger(app_with_a_span))

    span = next(r for r in caplog.records if r.message == "answer_question")
    request_line = next(r for r in caplog.records if r.message == "http_request")
    assert span.__dict__["request_id"] == request_line.__dict__["request_id"]


@pytest.mark.asyncio
async def test_a_failed_request_is_logged_and_still_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A crashed handler leaves a line and re-raises, so the 5xx is visible."""
    caplog.set_level(logging.INFO, logger="knowledge_bot.request")

    with pytest.raises(_HandlerError, match="handler exploded"):
        await _call(RequestLogger(_http_app(fail=True)))

    assert [record.message for record in caplog.records] == ["http_request_failed"]


@pytest.mark.asyncio
async def test_a_non_http_scope_passes_straight_through() -> None:
    """A lifespan or websocket scope is not logged as a request."""
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive, send
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        del message

    await RequestLogger(app)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]
