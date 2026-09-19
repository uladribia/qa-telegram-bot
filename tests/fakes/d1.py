# SPDX-License-Identifier: MIT
"""A SQLite-backed fake of the Cloudflare D1 binding.

D1 is SQLite, so running the real migrations and SQL against in-memory SQLite is
a faithful offline test of the repository SQL.
"""

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def schema_sql() -> str:
    """Return all migrations concatenated in order."""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    )


class FakeD1Result:
    """Minimal stand-in for a D1 result object."""

    def __init__(self, results: list[dict[str, object]]) -> None:
        """Create a result with the given rows."""
        self.results = results


class FakeD1Statement:
    """A prepared statement executed against SQLite."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: tuple[object, ...] = (),
    ) -> None:
        """Create a statement."""
        self._connection = connection
        self._sql = sql
        self._params = params

    def bind(self, *params: object) -> "FakeD1Statement":
        """Bind positional parameters."""
        return FakeD1Statement(self._connection, self._sql, params)

    async def first(self) -> dict[str, object] | None:
        """Return the first row, if any."""
        cursor = self._connection.execute(self._sql, self._params)
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    async def run(self) -> FakeD1Result:
        """Execute the statement and return any rows."""
        cursor = self._connection.execute(self._sql, self._params)
        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        self._connection.commit()
        return FakeD1Result(rows)


class FakeD1Database:
    """An in-memory SQLite database exposing the D1 binding surface."""

    def __init__(self, schema: str | None = None) -> None:
        """Create the database, applying migrations by default."""
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(schema_sql() if schema is None else schema)

    def prepare(self, sql: str) -> FakeD1Statement:
        """Prepare a statement."""
        return FakeD1Statement(self.connection, sql)
