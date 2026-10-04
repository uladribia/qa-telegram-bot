# SPDX-License-Identifier: MIT
"""Log-backed implementation of the tracing port.

A span is one line on the process log. Nothing is exported anywhere and no
third-party SDK is imported, so this works identically in the Worker, in the
local runtime, and in tests, at a cost of a single ``logging`` call per span.

Reconstruction rests on three things: the ``request_id`` context variable,
which every span opened while serving a request inherits; ``duration_ms``,
which orders the legs against each other; and the ``parent`` name recorded
when a span is opened inside another, which recovers the tree without a
collector.

Content is a property of the mode, not of the call site: ``CONTENT_FIELDS``
marks the values that carry a conversation, and they are dropped unless
content capture is on. Every other field is structural — ids, counts, sizes,
similarities, reasons — and safe in both modes.

Isolation is deliberately narrow. Only recording the span is guarded. An
exception raised by the *caller's* work inside the ``with`` block is the
caller's to handle, and a guard that swallowed it would turn a provider
outage into a crash.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from itertools import count
from logging import getLogger
from types import TracebackType

#: Logger every span line is written to.
_LOGGER = getLogger("knowledge_bot.span")

#: Identifies everything recorded while serving one request. Set by the HTTP
#: middleware, inherited by every span opened underneath it.
request_id: ContextVar[str] = ContextVar("request_id", default="-")

_request_ids = count(1)


def new_request_id() -> str:
    """Return an identifier for one unit of work.

    Derived from the clock and a counter rather than random bytes: the Workers
    runtime refuses entropy calls while a Worker is starting, and a
    correlation id does not need to be unpredictable.

    Returns:
        A short identifier, unique within this process.
    """
    return f"{time.time_ns() & 0xFFFFFFFF:x}{next(_request_ids):x}"


#: The innermost span currently open, so a nested span can name its parent.
current_span: ContextVar[str | None] = ContextVar("current_span", default=None)


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


@dataclass(frozen=True, slots=True)
class LogTracer:
    """Opens spans as log lines for the application layer.

    Attributes:
        capture: Whether conversation content may be recorded.
    """

    capture: bool

    def span(self, name: str, **fields: object) -> "LogSpan":
        """Open one named span.

        Args:
            name: The span name, in the existing ``snake_case`` style.
            **fields: Attributes describing the work about to happen.

        Returns:
            The open span, which records one line when it closes.
        """
        return LogSpan(name, safe(self.capture, fields))


@dataclass(slots=True)
class LogSpan:
    """One open span, recorded as a single line when it closes.

    Attributes:
        name: The span name, reused as the log event.
        fields: Every attribute recorded, open-time and via ``set_attributes``.
        started: When the span opened, for the duration on the line.
        token: The context token restoring the enclosing span on close.
    """

    name: str
    fields: dict[str, object] = field(default_factory=dict)
    started: float = field(default_factory=time.perf_counter)
    token: Token[str | None] | None = None

    @property
    def parent(self) -> str | None:
        """Return the name of the enclosing span, when there is one."""
        return current_span.get()

    def __enter__(self) -> "LogSpan":
        """Mark the span open and nest anything opened inside it.

        Returns:
            The open span.
        """
        self.token = current_span.set(self.name)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Record the span and let any caller exception through untouched.

        Args:
            exc_type: The propagating exception type, if any.
            exc: The propagating exception, if any.
            traceback: The propagating traceback, if any.

        Returns:
            ``False`` always. A failure inside the span is the caller's, and a
            failed recording must never become a raised exception.
        """
        if self.token is not None:
            current_span.reset(self.token)
        duration_ms = round((time.perf_counter() - self.started) * 1000, 2)
        _record(
            self.name,
            {
                "request_id": request_id.get(),
                "duration_ms": duration_ms,
                **({"parent": self.parent} if self.parent else {}),
                **self.fields,
            },
        )
        return False

    def set_attributes(self, fields: dict[str, object]) -> None:
        """Record fields discovered while the span was open.

        Args:
            fields: The attributes to add, merged over the earlier ones.
        """
        self.fields.update(fields)


def _record(name: str, fields: dict[str, object]) -> None:
    """Write one span line, never letting a logging failure reach the caller.

    Args:
        name: The span name, used as the event.
        fields: The attributes to record.
    """
    try:
        _LOGGER.info(name, extra=fields)
    except Exception:  # pragma: no cover - the logging module swallows these
        getLogger("knowledge_bot.telemetry").warning(
            "span_record_failed", extra={"span": name}
        )


@contextmanager
def bind_request_id(value: str) -> Iterator[None]:
    """Attach a correlation id to everything logged inside the block.

    Args:
        value: The identifier to bind.

    Yields:
        ``None``, for the duration of the block.
    """
    token = request_id.set(value)
    try:
        yield
    finally:
        request_id.reset(token)


def safe(capture: bool, fields: dict[str, object]) -> dict[str, object]:
    """Return the attributes that may be recorded in this mode.

    Args:
        capture: Whether content capture is on.
        fields: The attributes the caller recorded.

    Returns:
        The attributes to keep.
    """
    if capture:
        return dict(fields)
    return {name: value for name, value in fields.items() if name not in CONTENT_FIELDS}
