# SPDX-License-Identifier: MIT
"""SQLite execution adapter for shared SQL repositories."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from knowledge_bot.infrastructure.local.database import SQLiteDatabase
from knowledge_bot.infrastructure.sql.protocol import SqlResult, SqlStatement


@dataclass(frozen=True, slots=True)
class _SQLiteResult:
    """Result shape consumed by shared SQL repositories."""

    results: list[dict[str, object]]


class _SQLiteStatement:
    """Prepared statement backed by one aiosqlite connection."""

    def __init__(self, database: SQLiteDatabase, sql: str) -> None:
        """Store the database and SQL text."""
        self._database = database
        self._sql = sql
        self._parameters: tuple[object, ...] = ()

    def bind(self, *params: object) -> SqlStatement:
        """Bind positional parameters."""
        self._parameters = params
        return self

    async def first(self) -> dict[str, object] | None:
        """Return the first row as a dictionary."""
        cursor = await self._database.connection.execute(self._sql, self._parameters)
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def run(self) -> SqlResult:
        """Execute the statement and return all rows."""
        cursor = await self._database.connection.execute(self._sql, self._parameters)
        rows = await cursor.fetchall()
        return cast(SqlResult, _SQLiteResult([dict(row) for row in rows]))


class SQLiteBinding:
    """Expose the shared SQL execution protocol to SQLite."""

    def __init__(self, database: SQLiteDatabase) -> None:
        """Wrap the local database."""
        self.database = database

    def prepare(self, sql: str) -> SqlStatement:
        """Prepare one SQLite statement."""
        return _SQLiteStatement(self.database, sql)

    async def batch(self, statements: Sequence[SqlStatement]) -> object:
        """Execute statements atomically."""
        async with self.database.transaction():
            for statement in statements:
                await statement.run()
        return None
