# SPDX-License-Identifier: MIT
"""Async SQLite connection and local migration runner."""

import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite


class LocalDataError(ValueError):
    """Raised when local SQL data violates the runtime contract."""


class SQLiteDatabase:
    """One shared SQLite connection for the local application graph."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        """Wrap an existing aiosqlite connection."""
        self.connection = connection

    @classmethod
    async def connect(cls, path: str | Path) -> "SQLiteDatabase":
        """Open a connection and enable required SQLite pragmas."""
        connection = await aiosqlite.connect(str(path), isolation_level=None)
        connection.row_factory = sqlite3.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        return cls(connection)

    async def close(self) -> None:
        """Close the shared connection."""
        await self.connection.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run statements in one explicit transaction."""
        await self.connection.execute("BEGIN")
        try:
            yield self.connection
        except BaseException:
            await self.connection.rollback()
            raise
        else:
            await self.connection.commit()


def _statements(script: str) -> Iterator[str]:
    """Split a migration script using SQLite's statement parser."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                yield statement
            pending = ""
    if pending.strip():
        raise LocalDataError("migration contains an incomplete SQL statement")  # noqa: TRY003


async def apply_migrations(database: SQLiteDatabase, root: Path) -> None:
    """Apply shared and local SQL files exactly once, in lexical order."""
    await database.connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    await database.connection.commit()
    files = sorted((root / "migrations").glob("[0-9]*.sql")) + sorted(
        (root / "migrations" / "local").glob("[0-9]*.sql")
    )
    async with database.transaction() as connection:
        cursor = await connection.execute("SELECT name FROM schema_migrations")
        applied = {str(row[0]) for row in await cursor.fetchall()}
    for path in files:
        name = str(path.relative_to(root))
        if name in applied:
            continue
        async with database.transaction() as connection:
            for statement in _statements(await _read(path)):
                await connection.execute(statement)
            await connection.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (name, datetime.now(UTC).isoformat()),
            )


async def _read(path: Path) -> str:
    """Read one migration file."""
    return path.read_text(encoding="utf-8")
