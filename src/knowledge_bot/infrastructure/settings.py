# SPDX-License-Identifier: MIT
"""Application settings loaded from the environment."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_AI_MODELS: frozenset[str] = frozenset(
    {
        "@cf/google/embeddinggemma-300m",
        "@cf/zai-org/glm-4.7-flash",
    }
)


class Settings(BaseSettings):
    """Runtime configuration for the knowledge bot."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_bot_id: str = ""
    telegram_bot_username: str = ""
    allowed_telegram_chat_id: str = ""
    admin_telegram_user_id: str = ""
    internal_admin_key: str = ""

    embedding_model: str = "@cf/google/embeddinggemma-300m"
    generation_model: str = "@cf/zai-org/glm-4.7-flash"

    recap_enabled: bool = True
    recap_interval_hours: int = 24
    recap_language: str = "ca"

    background_listener_enabled: bool = False

    @model_validator(mode="after")
    def _validate_allowed_models(self) -> "Settings":
        """Reject any model outside the zero-cost allowlist."""
        for model in (self.embedding_model, self.generation_model):
            if model not in ALLOWED_AI_MODELS:
                message = f"Model not allowed under the zero-cost policy: {model}"
                raise ValueError(message)
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
