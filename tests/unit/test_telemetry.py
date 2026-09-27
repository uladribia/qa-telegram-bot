# SPDX-License-Identifier: MIT
"""Unit tests for the tracing port and its Logfire implementation."""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from types import TracebackType

import pytest

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.infrastructure.telemetry import (
    CONTENT_FIELDS,
    LogfireTracer,
    _safe,
)
from knowledge_bot.ports.telemetry import NoopTracer, Span

_SDK_DOWN = "the SDK is unavailable"


class _SdkUnavailableError(RuntimeError):
    """Raised by the fake SDK at the stage under test."""


class _FakeSpan:
    """A settable span, like the SDK's."""

    def __init__(self, name: str, sdk: "_FakeSdk") -> None:
        """Store the span name and the SDK recording its fields.

        Args:
            name: The span name.
            sdk: The fake SDK.
        """
        self.name = name
        self._sdk = sdk

    def set_attributes(self, fields: dict[str, object]) -> None:
        """Record fields set during the span.

        Args:
            fields: The attributes.
        """
        self._sdk.fields.append(fields)


class _FakeManager:
    """A context manager, like the SDK's ``span()`` return value."""

    def __init__(self, name: str, sdk: "_FakeSdk", fields: dict[str, object]) -> None:
        """Store what to record on entry.

        Args:
            name: The span name.
            sdk: The fake SDK.
            fields: The span attributes.
        """
        self._name = name
        self._sdk = sdk
        self._fields = fields

    def __enter__(self) -> Span:
        """Record the open, or fail when that is the stage under test.

        Returns:
            The open span.

        Raises:
            _SdkUnavailableError: When this fake fails on open.
        """
        if self._sdk.fail_on == "open":
            raise _SdkUnavailableError(_SDK_DOWN)
        self._sdk.opened.append((self._name, self._fields))
        return _FakeSpan(self._name, self._sdk)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Record the close, or fail when that is the stage under test.

        Args:
            exc_type: The propagating exception type, if any.
            exc: The propagating exception, if any.
            traceback: The propagating traceback, if any.

        Returns:
            ``False``, so nothing is suppressed.

        Raises:
            _SdkUnavailableError: When this fake fails on close.
        """
        if self._sdk.fail_on == "close":
            raise _SdkUnavailableError(_SDK_DOWN)
        self._sdk.closed.append(self._name)
        return False


@dataclass
class _FakeSdk:
    """A stand-in for the Logfire module, recording what it was asked to do."""

    fail_on: str | None = None
    opened: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    fields: list[dict[str, object]] = field(default_factory=list)

    def span(self, name: str, **fields: object) -> AbstractContextManager[Span]:
        """Return a context manager mimicking ``logfire.span``.

        Args:
            name: The span name.
            **fields: The span attributes.

        Returns:
            A context manager yielding a settable span.
        """
        return _FakeManager(name, self, fields)


def test_the_noop_tracer_records_nothing_and_never_fails() -> None:
    """The default tracer is usable and silent."""
    tracer = NoopTracer()

    with tracer.span("answer_question", reason="answered") as span:
        span.set_attributes({"mode": "synthesis"})


def test_structural_attributes_survive_and_content_is_dropped() -> None:
    """With content off, ids and counts are exported and text is not."""
    sdk = _FakeSdk()
    tracer = LogfireTracer(sdk=sdk, capture=False)

    with tracer.span(
        "generation",
        evidence_ids=["qa:1"],
        evidence_count=1,
        prompt="El text del missatge",
        question="Quan entrenen?",
    ):
        pass

    assert sdk.opened == [
        ("generation", {"evidence_ids": ["qa:1"], "evidence_count": 1})
    ]


def test_content_is_exported_when_capture_is_on() -> None:
    """With content on, the same span carries the conversation."""
    sdk = _FakeSdk()
    tracer = LogfireTracer(sdk=sdk, capture=True)

    with tracer.span("generation", prompt="El text", response_chars=10):
        pass

    assert sdk.opened[0][1]["prompt"] == "El text"


def test_a_caller_exception_is_not_swallowed_by_telemetry() -> None:
    """A provider error inside a span must reach the caller.

    Telemetry isolation guards opening and closing a span. A guard that also
    caught the caller's exceptions would turn a provider outage into a crash,
    because the mapping to the unavailable mode would never run.
    """
    tracer = LogfireTracer(sdk=_FakeSdk(), capture=False)

    with pytest.raises(ModelUnavailableError), tracer.span("retrieval"):
        raise ModelUnavailableError("embedding")


def test_a_span_that_cannot_open_degrades_to_a_silent_one() -> None:
    """An SDK failure to open is logged, and the work still happens."""
    tracer = LogfireTracer(sdk=_FakeSdk(fail_on="open"), capture=False)
    seen: list[str] = []

    with tracer.span("retrieval") as span:
        span.set_attributes({"evidence_count": 0})
        seen.append("work")

    assert seen == ["work"]


def test_a_close_failure_is_suppressed_when_nothing_else_failed() -> None:
    """A telemetry-only failure on close never reaches the caller."""
    tracer = LogfireTracer(sdk=_FakeSdk(fail_on="close"), capture=False)

    with tracer.span("answer_question"):
        pass


def test_a_close_failure_never_masks_a_real_error() -> None:
    """When the body failed, that error wins over a telemetry failure."""
    tracer = LogfireTracer(sdk=_FakeSdk(fail_on="close"), capture=False)

    with (
        pytest.raises(ValueError, match="real failure"),
        tracer.span("answer_question"),
    ):
        error = ValueError("real failure")
        raise error


def test_every_content_field_is_covered_by_the_filter() -> None:
    """A new content attribute must be added to the filter, not forgotten."""
    assert {"text", "question", "prompt", "answer", "messages", "texts"} <= set(
        CONTENT_FIELDS
    )
    assert _safe(False, {"text": "x", "duration_ms": 1}) == {"duration_ms": 1}
