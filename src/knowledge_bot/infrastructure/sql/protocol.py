# SPDX-License-Identifier: MIT
"""Structural SQL execution protocols shared by SQLite and D1."""

from collections.abc import Sequence
from typing import Protocol


class SqlResult(Protocol):
    """Rows returned by a SQL statement."""

    results: list[dict[str, object]]


class SqlStatement(Protocol):
    """A prepared SQL statement."""

    def bind(self, *params: object) -> "SqlStatement":
        """Bind positional parameters."""
        ...

    async def first(self) -> dict[str, object] | None:
        """Return the first row."""
        ...

    async def run(self) -> SqlResult:
        """Execute the statement."""
        ...


class SqlDatabase(Protocol):
    """Minimal database execution surface."""

    def prepare(self, sql: str) -> SqlStatement:
        """Prepare a statement."""
        ...

    async def batch(self, statements: Sequence[SqlStatement]) -> object:
        """Execute statements transactionally."""
        ...
