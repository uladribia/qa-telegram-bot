# SPDX-License-Identifier: MIT
"""Tests for application settings."""

import pytest
from pydantic import ValidationError

from knowledge_bot.infrastructure.settings import (
    ALLOWED_AI_MODELS,
    LOCAL_ALLOWED_AI_MODELS,
    RuntimeMode,
    Settings,
)


def test_default_models_are_allowlisted() -> None:
    """Default production models stay inside the zero-cost allowlist."""
    settings = Settings(_env_file=None)
    assert settings.embedding_model in ALLOWED_AI_MODELS
    assert settings.generation_model in ALLOWED_AI_MODELS


def test_budget_fractions_must_be_ordered() -> None:
    """Background work must stop before maintenance work."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            ai_background_budget_fraction=0.8,
            ai_maintenance_budget_fraction=0.7,
        )


def test_local_models_use_local_allowlist() -> None:
    """Local runtime accepts only the configured Ollama models."""
    settings = Settings(
        _env_file=None,
        runtime=RuntimeMode.LOCAL,
        embedding_model="embeddinggemma",
        generation_model="gemma3:270m",
    )
    assert settings.embedding_model in LOCAL_ALLOWED_AI_MODELS
    assert settings.generation_model in LOCAL_ALLOWED_AI_MODELS


def test_cloudflare_rejects_content_logging() -> None:
    """Cloudflare runtime cannot enable content logging."""
    with pytest.raises(ValidationError, match="KB_LOG_CONTENT"):
        Settings(_env_file=None, log_content=True)


def test_ai_deadlines_are_bounded() -> None:
    """AI adapter deadlines must be positive and below Telegram's hard ceiling."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_generation_timeout_seconds=56)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_embed_timeout_seconds=0)
