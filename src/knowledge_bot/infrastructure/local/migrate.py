# SPDX-License-Identifier: MIT
"""CLI entrypoint for applying local SQLite migrations."""

import asyncio
from pathlib import Path

from knowledge_bot.infrastructure.local.database import SQLiteDatabase, apply_migrations
from knowledge_bot.infrastructure.settings import Settings


async def _run() -> None:
    """Apply all shared and local migrations to the configured database."""
    settings = Settings()
    database = await SQLiteDatabase.connect(settings.sqlite_path)
    try:
        await apply_migrations(database, Path.cwd())
    finally:
        await database.close()


def main() -> None:
    """Run the local migration command."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
