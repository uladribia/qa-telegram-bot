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


def test_content_capture_is_a_separate_switch_from_sending() -> None:
    """Telemetry can be captured locally without being sent anywhere.

    Content capture is the debugging switch; sending is its own switch. They
    are separate settings on purpose, so a run can record content for the
    console while exporting nothing.
    """
    settings = Settings(
        _env_file=None,
        logfire_capture_content=True,
        logfire_send_to_logfire=False,
    )
    assert settings.logfire_capture_content is True
    assert settings.logfire_send_to_logfire is False


def test_the_removed_content_logging_setting_is_gone() -> None:
    """``KB_LOG_CONTENT`` no longer exists; the capture flag replaced it."""
    assert "log_content" not in Settings.model_fields
    assert not any(
        "KB_LOG_CONTENT" in str(alias)
        for aliases in Settings.model_fields.values()
        for alias in (aliases.alias or ())
    )


def test_ai_deadlines_are_bounded() -> None:
    """AI adapter deadlines must be positive and below Telegram's hard ceiling."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_generation_timeout_seconds=56)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_embed_timeout_seconds=0)
