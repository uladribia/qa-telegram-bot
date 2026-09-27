# SPDX-License-Identifier: MIT
"""Unit tests for the single logging configuration and its formatters."""

import json
import logging
import sys
from collections.abc import Iterator
from types import TracebackType

import pytest

from knowledge_bot.infrastructure import logging as logging_config
from knowledge_bot.infrastructure.logging import (
    _JsonFormatter,
    _TextFormatter,
    configure_logging,
    content_capture_enabled,
    log_content,
)

type ExcInfo = tuple[type[BaseException], BaseException, TracebackType]


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
    """A content event never reaches telemetry when capture is off."""
    monkeypatch.setattr(logging_config, "_capture_content", False)
    monkeypatch.setattr(logging_config, "_observability_configured", True)

    assert content_capture_enabled() is False

    log_content("telegram_outbound", text="must not be exported")
