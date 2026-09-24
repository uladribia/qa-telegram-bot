# SPDX-License-Identifier: MIT
"""SQLite compatibility for the existing purpose-specific SQL repositories."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from knowledge_bot.infrastructure.cloudflare.d1 import D1Result, D1Statement
from knowledge_bot.infrastructure.local.database import SQLiteDatabase


@dataclass(frozen=True, slots=True)
class _SQLiteResult:
    """Result shape consumed by the SQL repository adapters."""

    results: list[dict[str, object]]


class _SQLiteStatement:
    """Prepared statement backed by one aiosqlite connection."""

    def __init__(self, database: SQLiteDatabase, sql: str) -> None:
        """Store the database and SQL text."""
        self._database = database
        self._sql = sql
        self._parameters: tuple[object, ...] = ()

    def bind(self, *params: object) -> D1Statement:
        """Bind positional parameters."""
        self._parameters = params
        return self

    async def first(self) -> dict[str, object] | None:
        """Return the first row as a dictionary."""
        cursor = await self._database.connection.execute(self._sql, self._parameters)
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def run(self) -> D1Result:
        """Execute the statement and return all rows."""
        cursor = await self._database.connection.execute(self._sql, self._parameters)
        rows = await cursor.fetchall()
        return cast(D1Result, _SQLiteResult([dict(row) for row in rows]))


class SQLiteBinding:
    """Expose the small D1 statement contract to shared SQL repositories.

    The repository classes still contain the existing purpose-specific SQL and
    transactions. This adapter only supplies SQLite execution; it is temporary
    infrastructure until those repositories are split into local and D1 modules.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        """Wrap the shared local database."""
        self.database = database

    def prepare(self, sql: str) -> D1Statement:
        """Prepare one SQLite statement."""
        return _SQLiteStatement(self.database, sql)

    async def batch(self, statements: Sequence[D1Statement]) -> object:
        """Execute statements atomically."""
        async with self.database.transaction():
            for statement in statements:
                await statement.run()
        return None
