# SPDX-License-Identifier: MIT
"""Application settings loaded from the environment."""

# ruff: noqa: TRY003

from enum import StrEnum
from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_AI_MODELS = frozenset(
    {
        "@cf/google/embeddinggemma-300m",
        "@cf/zai-org/glm-4.7-flash",
        "@cf/mistralai/mistral-small-3.1-24b-instruct",
        "@cf/baai/bge-reranker-base",
    }
)
LOCAL_ALLOWED_AI_MODELS = frozenset({"embeddinggemma", "gemma3:270m"})


class RuntimeMode(StrEnum):
    """Supported application runtimes."""

    LOCAL = "local"
    CLOUDFLARE = "cloudflare"


class Settings(BaseSettings):
    """Runtime configuration for the knowledge bot."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True
    )
    runtime: RuntimeMode = Field(
        default=RuntimeMode.CLOUDFLARE,
        validation_alias=AliasChoices("KB_RUNTIME", "RUNTIME"),
    )
    sqlite_path: str = Field(
        default="./data/knowledge-bot.sqlite3",
        validation_alias=AliasChoices("KB_SQLITE_PATH", "SQLITE_PATH"),
    )
    ollama_base_url: str = "http://127.0.0.1:11434"
    log_content: bool = Field(
        default=False, validation_alias=AliasChoices("KB_LOG_CONTENT", "LOG_CONTENT")
    )

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_bot_id: str = ""
    telegram_bot_username: str = ""
    allowed_telegram_user_ids: str = ""
    admin_telegram_user_id: str = ""
    internal_admin_key: str = ""

    embedding_model: str = "@cf/google/embeddinggemma-300m"
    generation_model: str = "@cf/mistralai/mistral-small-3.1-24b-instruct"
    reranker_model: str = "@cf/baai/bge-reranker-base"
    ai_embed_timeout_seconds: float = Field(default=10.0, gt=0, le=55)
    ai_generation_timeout_seconds: float = Field(default=35.0, gt=0, le=55)
    ai_generation_max_tokens: int = Field(default=1024, gt=0)

    reviewer_escalation_timeout_seconds: int = 86_400
    pairing_question_window_minutes: int = 5
    pairing_max_pending_questions: int = 5
    background_listener_enabled: bool = False
    classifier_confidence_threshold: float = 0.60
    classifier_margin_threshold: float = 0.15
    classifier_model_path: str = "data/classifier/model.json"
    answer_similarity_floor: float = 0.45
    qa_top_k: int = 5
    message_top_k: int = 4
    ai_daily_neuron_budget: float = 10_000.0
    ai_neuron_reserve_fraction: float = 0.25
    ai_background_budget_fraction: float = 0.50
    ai_maintenance_budget_fraction: float = 0.70
    ai_embed_neurons_per_char: float = 0.015
    ai_chat_neurons_per_char: float = 0.020

    @model_validator(mode="after")
    def _validate_allowed_models(self) -> "Settings":
        """Reject models outside the active runtime allowlist."""
        local = self.runtime is RuntimeMode.LOCAL
        allowed = LOCAL_ALLOWED_AI_MODELS if local else ALLOWED_AI_MODELS
        # The local runtime has no cross-encoder, so the reranker is unset
        # there and retrieval falls back to RRF order.
        checked = [self.embedding_model, self.generation_model]
        if not local:
            checked.append(self.reranker_model)
        for model in checked:
            if model not in allowed:
                raise ValueError(
                    f"Model not allowed under the zero-cost policy: {model}"
                )
        return self

    @model_validator(mode="after")
    def _validate_runtime_values(self) -> "Settings":
        """Validate thresholds, intervals, and budget settings."""
        for name in (
            "answer_similarity_floor",
            "classifier_confidence_threshold",
            "classifier_margin_threshold",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.qa_top_k < 1 or self.message_top_k < 1:
            raise ValueError("top-k values must be at least 1")
        if (
            self.reviewer_escalation_timeout_seconds < 0
            or self.pairing_question_window_minutes <= 0
            or self.pairing_max_pending_questions <= 0
        ):
            raise ValueError("intervals must be valid")
        if self.ai_daily_neuron_budget <= 0:
            raise ValueError("AI daily budget must be positive")
        if (
            not 0
            < self.ai_background_budget_fraction
            < self.ai_maintenance_budget_fraction
            < 1
        ):
            raise ValueError("budget fractions must be ordered")
        if self.runtime is RuntimeMode.CLOUDFLARE and self.log_content:
            raise ValueError("KB_LOG_CONTENT cannot be enabled in Cloudflare mode")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
