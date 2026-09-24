# SPDX-License-Identifier: MIT
"""Tests for application settings."""

import pytest
from pydantic import ValidationError

from knowledge_bot.infrastructure.settings import ALLOWED_AI_MODELS, Settings


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
