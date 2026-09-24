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
    """Default models stay inside the zero-cost allowlist."""
    settings = Settings(_env_file=None)
    assert settings.embedding_model in ALLOWED_AI_MODELS
    assert settings.generation_model in ALLOWED_AI_MODELS


def test_allowed_chat_ids_parses_csv() -> None:
    """The multi-group allowlist parses a comma-separated list."""
    settings = Settings(
        _env_file=None,
        allowed_telegram_chat_ids=" -100 , -200,,",
    )
    assert settings.allowed_chat_ids == ("-100", "-200")


def test_allowed_chat_ids_empty_yields_nothing() -> None:
    """An empty allowlist means no group is served."""
    settings = Settings(_env_file=None)
    assert settings.allowed_chat_ids == ()


def test_budget_fractions_must_be_ordered() -> None:
    """Background work must stop before maintenance work."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            ai_background_budget_fraction=0.8,
            ai_maintenance_budget_fraction=0.7,
        )


def test_local_models_use_local_allowlist() -> None:
    """Local runtime accepts only the configured zero-cost Ollama models."""
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
