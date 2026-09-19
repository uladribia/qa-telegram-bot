# SPDX-License-Identifier: MIT
"""Tests for application settings."""

from knowledge_bot.infrastructure.settings import ALLOWED_AI_MODELS, Settings


def test_default_models_are_allowlisted() -> None:
    """Default models stay inside the zero-cost allowlist."""
    settings = Settings(_env_file=None)
    assert settings.embedding_model in ALLOWED_AI_MODELS
    assert settings.generation_model in ALLOWED_AI_MODELS
