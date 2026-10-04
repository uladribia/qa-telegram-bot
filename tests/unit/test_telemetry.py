# SPDX-License-Identifier: MIT
"""Unit tests for the tracing port and its log-backed implementation."""

import logging
import re
import sys

import pytest

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.infrastructure.logging import (
    SECRET_VALUE_PATTERNS,
    _JsonFormatter,
    _scrub,
)
from knowledge_bot.infrastructure.telemetry import (
    CONTENT_FIELDS,
    LogTracer,
    bind_request_id,
    new_request_id,
    safe,
)
from knowledge_bot.ports.telemetry import NoopTracer


@pytest.fixture
def recorded(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture span lines at INFO without touching the process handlers."""
    caplog.set_level(logging.INFO, logger="knowledge_bot.span")
    return caplog


def field_of(record: logging.LogRecord, name: str) -> object:
    """Return one contextual field a span attached to its record.

    Reads the record dictionary directly: the attribute name is decided at
    runtime by the caller, so it is not part of ``LogRecord``'s own shape.

    Args:
        record: The captured record.
        name: The field to read.

    Returns:
        The recorded value.
    """
    return record.__dict__[name]


def test_the_noop_tracer_records_nothing_and_never_fails() -> None:
    """The default tracer is usable and silent."""
    tracer = NoopTracer()

    with tracer.span("answer_question", reason="answered") as span:
        span.set_attributes({"mode": "synthesis"})


def test_a_span_writes_one_line_naming_itself_and_its_duration(
    recorded: pytest.LogCaptureFixture,
) -> None:
    """A closed span is a single event carrying everything it recorded."""
    tracer = LogTracer(capture=False)

    with tracer.span("generation", evidence_count=2) as span:
        span.set_attributes({"status": "ok"})

    assert [record.message for record in recorded.records] == ["generation"]
    fields = recorded.records[0].__dict__
    assert fields["evidence_count"] == 2
    assert fields["status"] == "ok"
    assert fields["duration_ms"] >= 0


def test_structural_attributes_survive_and_content_is_dropped() -> None:
    """With content off, ids and counts are recorded and text is not."""
    fields = safe(
        False,
        {
            "evidence_ids": ["qa:1"],
            "evidence_count": 1,
            "prompt": "El text del missatge",
            "question": "Quan entrenen?",
        },
    )

    assert fields == {"evidence_ids": ["qa:1"], "evidence_count": 1}


def test_content_is_recorded_when_capture_is_on() -> None:
    """With content on, the same span carries the conversation."""
    assert safe(True, {"prompt": "El text", "duration_ms": 1}) == {
        "prompt": "El text",
        "duration_ms": 1,
    }


def test_a_caller_exception_is_not_swallowed_by_telemetry() -> None:
    """A provider error inside a span must reach the caller.

    Recording a span must never become the reason a flow fails differently,
    because the mapping to the unavailable mode would never run.
    """
    tracer = LogTracer(capture=False)

    with pytest.raises(ModelUnavailableError), tracer.span("retrieval"):
        raise ModelUnavailableError("embedding")


def test_a_nested_span_names_its_parent(
    recorded: pytest.LogCaptureFixture,
) -> None:
    """The tree is recoverable from the lines alone, with no collector."""
    tracer = LogTracer(capture=False)

    with tracer.span("answer_question"), tracer.span("retrieval"):
        pass

    parents = {
        record.message: record.__dict__.get("parent") for record in recorded.records
    }
    assert parents["retrieval"] == "answer_question"
    assert parents["answer_question"] is None


def test_spans_share_the_bound_request_id(
    recorded: pytest.LogCaptureFixture,
) -> None:
    """Everything logged under one request carries that request's id."""
    tracer = LogTracer(capture=False)
    identifier = new_request_id()

    with (
        bind_request_id(identifier),
        tracer.span("answer_question"),
        tracer.span("generation"),
    ):
        pass

    assert {field_of(record, "request_id") for record in recorded.records} == {
        identifier
    }


def test_the_request_id_is_restored_after_the_block(
    recorded: pytest.LogCaptureFixture,
) -> None:
    """Binding a request id does not leak into the next unit of work."""
    tracer = LogTracer(capture=False)

    with bind_request_id(new_request_id()):
        pass
    with tracer.span("answer_question"):
        pass

    assert field_of(recorded.records[0], "request_id") == "-"


def test_request_ids_are_distinct() -> None:
    """Two requests never share a correlation id."""
    assert new_request_id() != new_request_id()


def test_every_content_field_is_covered_by_the_filter() -> None:
    """A new content attribute must be added to the filter, not forgotten."""
    assert {"text", "question", "prompt", "answer", "messages", "texts"} <= set(
        CONTENT_FIELDS
    )
    assert safe(False, {"text": "x", "duration_ms": 1}) == {"duration_ms": 1}


def test_a_bot_token_never_reaches_the_log() -> None:
    """A credential riding inside a value is scrubbed on the way out.

    The Telegram transport puts the bot token in the request URL, where no key
    name reveals it, so the value shape is the only defence.
    """
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    record = logging.LogRecord(
        name="knowledge_bot.delivery",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="delivery",
        args=(),
        exc_info=None,
    )
    record.url = f"https://api.telegram.org/bot{token}/sendMessage"

    rendered = _JsonFormatter().format(record)

    assert token not in rendered
    assert "[scrubbed]" in rendered


class _DeliveryError(RuntimeError):
    """Stands in for the HTTP error an outbound client raises."""


def test_a_bot_token_in_a_traceback_never_reaches_the_log() -> None:
    """An HTTP error carries the URL, so the traceback is scrubbed too."""
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"

    def failing_call() -> None:
        """Fail the way an HTTP client does, with the URL in the message."""
        # The long message is the point: it is what carries the URL.
        raise _DeliveryError(  # noqa: TRY003
            f"POST https://api.telegram.org/bot{token}/x failed"
        )

    try:
        failing_call()
    except _DeliveryError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="knowledge_bot.delivery",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="delivery_failed",
        args=(),
        exc_info=exc_info,
    )

    rendered = _JsonFormatter().format(record)

    assert token not in rendered


def test_the_token_pattern_still_matches_a_real_token() -> None:
    """Scrubbing is not vacuous: the pattern matches what it claims to."""
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    assert re.search("|".join(SECRET_VALUE_PATTERNS), token)


def test_a_normal_url_is_left_alone() -> None:
    """Scrubbing must not mangle the fields an operator needs to read."""
    assert _scrub("https://api.telegram.org/bot123/sendMessage") == (
        "https://api.telegram.org/bot123/sendMessage"
    )
