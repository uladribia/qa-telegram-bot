# SPDX-License-Identifier: MIT
"""Tracing port.

The application layer records what it decided without knowing that Logfire
exists: it opens named spans and attaches fields through this protocol, and
the infrastructure supplies the implementation. Tests get the no-op default,
which costs nothing and cannot export anything.
"""

from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable


@runtime_checkable
class Span(Protocol):
    """One open span."""

    name: str

    def set_attributes(self, fields: dict[str, object]) -> None:
        """Attach fields discovered while the span was open.

        Args:
            fields: The attributes to add.
        """
        ...


@runtime_checkable
class Tracer(Protocol):
    """Records named spans around a unit of work."""

    def span(self, name: str, **fields: object) -> AbstractContextManager[Span]:
        """Open a span.

        Args:
            name: The span name, in the existing ``snake_case`` style.
            **fields: Attributes describing the work about to happen.

        Returns:
            A context manager yielding the open span, closing it on exit.
        """
        ...


class _NoopSpan:
    """A span that records nothing, used when no tracer is configured."""

    def __init__(self, name: str) -> None:
        """Store the name so the span is indistinguishable in shape.

        Args:
            name: The span name.
        """
        self.name = name

    def __enter__(self) -> "_NoopSpan":
        """Enter the span.

        Returns:
            The span itself.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Leave the span, discarding anything that happened inside.

        Args:
            *exc_info: The exception triple, if any.
        """

    def set_attributes(self, fields: dict[str, object]) -> None:
        """Discard the fields.

        Args:
            fields: Ignored.
        """


class NoopTracer:
    """The default tracer: correct, free, and invisible."""

    def span(self, name: str, **fields: object) -> AbstractContextManager[Span]:
        """Open a span that records nothing.

        Args:
            name: The span name.
            **fields: Ignored.

        Returns:
            A context manager yielding a span that discards everything.
        """
        return _NoopSpan(name)
