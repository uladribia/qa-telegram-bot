# SPDX-License-Identifier: MIT
"""Integration coverage for the local application composition."""

from pathlib import Path

import pytest

from knowledge_bot.application.answer_policy import AnswerPolicy
from knowledge_bot.infrastructure.local.composition import build_context
from knowledge_bot.infrastructure.settings import (
    DecisionBackend,
    RuntimeMode,
    Settings,
)

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


@pytest.mark.asyncio
async def test_local_composition_wires_the_shipped_decision_path(
    tmp_path: Path,
) -> None:
    """The local graph must decide the same three things production decides.

    Mirroring is the point: if the local runtime silently took a different
    path, every end-to-end run would be exercising a system nobody ships, and
    the smoke would keep passing while meaning nothing. Both configurations are
    built in one event loop because each graph owns a SQLite worker thread.
    """
    from knowledge_bot.application.answer_decisions import AnswerDecisionGate
    from knowledge_bot.application.assessment import (
        BaselineAssessmentModel,
        SystemOneAnswerDecisionModel,
        SystemOneAssessmentModel,
    )

    shipped = Settings(
        _env_file=None,
        runtime=RuntimeMode.LOCAL,
        sqlite_path=str(tmp_path / "mirror.sqlite3"),
        embedding_model="embeddinggemma",
        generation_model="gemma3:270m",
        ollama_base_url="http://127.0.0.1:11434",
        decision_backend=DecisionBackend.SYSTEM_ONE,
        decision_model="tev1:0.8b",
    )
    context, database, client = await build_context(shipped)
    try:
        assert isinstance(context.listener.assessment, SystemOneAssessmentModel)
        assert context.listener.assessment.model == "tev1:0.8b"
        assert context.listener.assessment.include_relevance is False
        assert context.listener.assessment.fallback is not None
        assert isinstance(context.pairing.assessment, SystemOneAssessmentModel)
        gate = context.answer.decisions
        assert isinstance(gate, AnswerDecisionGate)
        assert isinstance(gate.model, SystemOneAnswerDecisionModel)
        assert gate.settings.sufficiency_threshold == 0.50
        assert gate.settings.selection_threshold == 0.90
        # One transport, one HTTP client, one model name: the listener and the
        # answer path share the decision service instead of opening two.
        assert gate.model.transport is context.listener.assessment.transport
    finally:
        await client.aclose()
        await database.close()

    rollback = Settings(
        _env_file=None,
        runtime=RuntimeMode.LOCAL,
        sqlite_path=str(tmp_path / "rollback.sqlite3"),
        embedding_model="embeddinggemma",
        generation_model="gemma3:270m",
        ollama_base_url="http://127.0.0.1:11434",
        decision_backend=DecisionBackend.BASELINE,
    )
    rolled, rollback_database, rollback_client = await build_context(rollback)
    try:
        assert isinstance(rolled.listener.assessment, BaselineAssessmentModel)
        assert not isinstance(rolled.listener.assessment, SystemOneAssessmentModel)
        assert rolled.answer.decisions is None
        assert isinstance(rolled.answer.policy, AnswerPolicy)
        assert rolled.pairing.assessment is rolled.listener.assessment
    finally:
        await rollback_client.aclose()
        await rollback_database.close()
