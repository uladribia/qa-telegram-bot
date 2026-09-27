# SPDX-License-Identifier: MIT
"""Logfire implementation of the tracing port.

Content is a property of the mode, not of the call site: ``CONTENT_FIELDS``
marks the values that carry a conversation, and they are dropped unless
content capture is on. Every other attribute is structural — ids, counts,
sizes, similarities, reasons — and safe in both modes.

Isolation is deliberately narrow. Only opening and closing a span is guarded.
An exception raised by the *caller's* work inside the ``with`` block is the
caller's to handle, and a guard that swallowed it would turn a provider outage
into a crash.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from logging import getLogger
from types import ModuleType, TracebackType
from typing import Protocol, cast, runtime_checkable

from knowledge_bot.ports.telemetry import Span


def _as_sdk(module: ModuleType) -> "TelemetrySdk":
    """Narrow a lazily imported module to the SDK surface used here.

    Args:
        module: The imported Logfire module.

    Returns:
        The module, typed as the SDK protocol.
    """
    return cast("TelemetrySdk", module)


#: Attribute names whose values are conversation content rather than structure.
CONTENT_FIELDS = frozenset(
    {
        "answer",
        "content",
        "evidence_text",
        "messages",
        "prompt",
        "question",
        "sender_name",
        "text",
        "texts",
    }
)


@runtime_checkable
class TelemetrySdk(Protocol):
    """The part of the Logfire SDK this module uses.

    The SDK is imported lazily and only exists once telemetry is configured,
    so it is held as this protocol rather than as its concrete module type.
    """

    def span(self, name: str, **fields: object) -> AbstractContextManager[Span]:
        """Open a named span.

        Args:
            name: The span name.
            **fields: The span attributes.

        Returns:
            A context manager yielding the open span.
        """


@dataclass(frozen=True, slots=True)
class LogfireTracer:
    """Opens spans on the Logfire SDK for the application layer.

    Attributes:
        sdk: The SDK, or ``None`` when it could not be imported.
        capture: Whether conversation content may be exported.
    """

    sdk: TelemetrySdk | None
    capture: bool

    def span(self, name: str, **fields: object) -> AbstractContextManager[Span]:
        """Open one named span, isolating any SDK failure.

        Args:
            name: The span name.
            **fields: Attributes describing the work about to happen.

        Returns:
            A context manager yielding the open span.
        """
        return _IsolatedSpan(self, name, _safe(self.capture, fields))


class _IsolatedSpan:
    """A span that never lets a telemetry failure reach the caller."""

    def __init__(
        self, tracer: LogfireTracer, name: str, fields: dict[str, object]
    ) -> None:
        """Store what is needed to open the span later.

        Args:
            tracer: The tracer holding the SDK and the capture mode.
            name: The span name.
            fields: The attributes, already filtered for the capture mode.
        """
        self._tracer = tracer
        self._name = name
        self._fields = fields
        self._open: AbstractContextManager[Span] | None = None

    def __enter__(self) -> Span:
        """Open the SDK span, degrading to one that records nothing.

        Returns:
            The open span.
        """
        if self._tracer.sdk is None:
            return _DiscardingSpan(self._name)
        try:
            self._open = self._tracer.sdk.span(self._name, **self._fields)
            return self._open.__enter__()
        except Exception as error:
            self._warn(error)
            return _DiscardingSpan(self._name)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Close the span without letting a close failure mask a real error.

        Args:
            exc_type: The propagating exception type, if any.
            exc: The propagating exception, if any.
            traceback: The propagating traceback, if any.

        Returns:
            ``True`` only to suppress a telemetry-only failure.
        """
        if self._open is None:
            return False
        try:
            return bool(self._open.__exit__(exc_type, exc, traceback))
        except Exception as error:
            self._warn(error)
            return exc_type is None

    def _warn(self, error: Exception) -> None:
        """Record that telemetry failed, without the failure itself.

        Args:
            error: The exception the SDK raised.
        """
        getLogger("knowledge_bot.telemetry").warning(
            "logfire_span_failed",
            extra={"span": self._name, "error": type(error).__name__},
        )


def _safe(capture: bool, fields: dict[str, object]) -> dict[str, object]:
    """Return the attributes that may be exported in this mode.

    Args:
        capture: Whether content capture is on.
        fields: The attributes the caller recorded.

    Returns:
        The attributes to export.
    """
    if capture:
        return fields
    return {name: value for name, value in fields.items() if name not in CONTENT_FIELDS}


class _DiscardingSpan:
    """A span used when the SDK is unavailable, so nothing is recorded."""

    def __init__(self, name: str) -> None:
        """Store the span name.

        Args:
            name: The span name.
        """
        self.name = name

    def set_attributes(self, fields: dict[str, object]) -> None:
        """Discard the fields.

        Args:
            fields: Ignored.
        """
