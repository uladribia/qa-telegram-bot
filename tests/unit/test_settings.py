# SPDX-License-Identifier: MIT
"""Tests for application settings."""

import pytest
from pydantic import ValidationError

from knowledge_bot.infrastructure.settings import (
    ALLOWED_AI_MODELS,
    LOCAL_ALLOWED_AI_MODELS,
    DecisionBackend,
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


def test_the_decision_model_is_the_shipped_path() -> None:
    """Defaults must keep deciding with the model, not silently revert.

    A rollback is one environment variable and a deliberate act. If the
    default ever flips back to the baseline, nobody notices until the bot is
    quietly answering with a classifier whose own gate fails.
    """
    settings = Settings(_env_file=None)

    assert settings.decision_backend is DecisionBackend.SYSTEM_ONE
    assert settings.decision_model == "@cf/cloudflare/clef-flash"
    assert settings.decision_include_relevance is False
    assert settings.decision_answer_path is True
    assert settings.decision_fallback_to_baseline is True
    assert settings.decision_sufficiency_threshold == 0.50
    assert settings.decision_selection_threshold == 0.90


def test_the_decision_model_is_allowlisted_for_production() -> None:
    """The shipped decision model must pass the same allowlist as everything."""
    settings = Settings(_env_file=None)

    assert settings.decision_model in ALLOWED_AI_MODELS


def test_the_rollback_is_reachable_without_touching_code() -> None:
    """`baseline` must be a valid configuration, not a removed branch."""
    settings = Settings(_env_file=None, decision_backend="baseline")

    assert settings.decision_backend is DecisionBackend.BASELINE
    assert settings.decision_model  # still configured, simply unused


def test_a_decision_deadline_is_not_a_generation_deadline() -> None:
    """A decision sits in front of an answer; it is not waited on like a model."""
    settings = Settings(_env_file=None)

    assert settings.ai_decision_timeout_seconds <= 10
    assert settings.ai_decision_timeout_seconds < settings.ai_generation_timeout_seconds


def test_decision_thresholds_are_probabilities() -> None:
    """A threshold outside [0, 1] would decide by construction."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, decision_sufficiency_threshold=1.4)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, decision_selection_threshold=-0.2)
