# SPDX-License-Identifier: MIT
"""Explicit local System-One decision service smoke test."""

import os

import httpx
import pytest

from knowledge_bot.application.assessment import SystemOneAssessmentModel
from knowledge_bot.infrastructure.local.system_one import HttpSystemOneTransport
from knowledge_bot.infrastructure.settings import Settings

pytestmark = pytest.mark.e2e_local


@pytest.mark.asyncio
async def test_local_decision_service_answers_one_request() -> None:
    """The listener depends on this: prove the contract before using it.

    This is the runtime gate, not a quality gate. It proves the service
    answers the canonical System-One request and that the application's strict
    parser accepts the answer, which is exactly what the listener will ask for
    on every unaddressed message.
    """
    if os.getenv("RUN_LOCAL_AI_E2E") != "1":
        pytest.skip("set RUN_LOCAL_AI_E2E=1 to call the local decision service")
    settings = Settings(_env_file=".env.local")
    transport = HttpSystemOneTransport(
        client=httpx.AsyncClient(timeout=30.0),
        base_url=settings.decision_base_url,
        timeout_seconds=30.0,
    )
    model = SystemOneAssessmentModel(transport=transport, model=settings.decision_model)
    try:
        await transport.client.get(
            f"{settings.decision_base_url.rstrip('/')}/api/tags",
        )
    except httpx.HTTPError:
        await transport.client.aclose()
        pytest.skip("local decision service is not running; run make dev-up")

    assessment = await model.assess("Quan entrenen els entrenaments?")
    await transport.client.aclose()

    assert assessment.classification.best_label is not None
    assert 0.0 <= assessment.classification.best_score <= 1.0
    assert assessment.classification.embedding == ()
    assert assessment.pair_relevance == {}
