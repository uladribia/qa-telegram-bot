# SPDX-License-Identifier: MIT
"""Integration coverage for the local application composition."""

from pathlib import Path

import pytest

from knowledge_bot.infrastructure.local.composition import build_context
from knowledge_bot.infrastructure.settings import RuntimeMode, Settings

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_local_composition_builds_without_cloudflare(tmp_path: Path) -> None:
    """Build the complete local graph using SQLite and no Cloudflare bindings."""
    settings = Settings(
        _env_file=None,
        runtime=RuntimeMode.LOCAL,
        sqlite_path=str(tmp_path / "knowledge.sqlite3"),
        embedding_model="embeddinggemma",
        generation_model="gemma3:270m",
        ollama_base_url="http://127.0.0.1:11434",
    )
    context, database, client = await build_context(settings)
    try:
        assert context.settings.runtime is RuntimeMode.LOCAL
        assert context.answer is not None
    finally:
        await client.aclose()
        await database.close()
