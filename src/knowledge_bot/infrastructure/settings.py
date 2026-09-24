# SPDX-License-Identifier: MIT
"""Application settings loaded from the environment."""

from enum import StrEnum
from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_AI_MODELS: frozenset[str] = frozenset(
    {
        "@cf/google/embeddinggemma-300m",
        "@cf/zai-org/glm-4.7-flash",
    }
)
LOCAL_ALLOWED_AI_MODELS: frozenset[str] = frozenset({"embeddinggemma", "gemma3:270m"})


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
        default=False,
        validation_alias=AliasChoices("KB_LOG_CONTENT", "LOG_CONTENT"),
    )

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_bot_id: str = ""
    telegram_bot_username: str = ""
    allowed_telegram_chat_ids: str = ""
    allowed_telegram_user_ids: str = ""
    admin_telegram_user_id: str = ""
    internal_admin_key: str = ""

    embedding_model: str = "@cf/google/embeddinggemma-300m"
    generation_model: str = "@cf/zai-org/glm-4.7-flash"

    recap_enabled: bool = True
    recap_interval_hours: int = 24
    recap_language: str = "ca"

    admin_report_mode: str = "always"
    admin_report_interval_min: int = 60

    background_listener_enabled: bool = False

    classifier_chitchat_discard_threshold: float = 0.80
    classifier_keep_signal_threshold: float = 0.45
    classifier_question_match_threshold: float = 0.60
    classifier_answer_match_threshold: float = 0.55

    direct_qa_threshold: float = 0.7
    synthesis_threshold: float = 0.3
    qa_top_k: int = 5
    message_top_k: int = 4

    ai_daily_neuron_budget: float = 10_000.0
    ai_neuron_reserve_fraction: float = 0.25
    ai_background_budget_fraction: float = 0.50
    ai_maintenance_budget_fraction: float = 0.70
    ai_embed_neurons_per_char: float = 0.015
    ai_chat_neurons_per_char: float = 0.020

    @property
    def allowed_chat_ids(self) -> tuple[str, ...]:
        """Return the allowed Telegram group chat ids.

        ``ALLOWED_TELEGRAM_CHAT_IDS`` is a comma-separated list; one entry
        per group the bot serves.
        """
        return tuple(
            chat_id.strip()
            for chat_id in self.allowed_telegram_chat_ids.split(",")
            if chat_id.strip()
        )

    @model_validator(mode="after")
    def _validate_allowed_models(self) -> "Settings":
        """Reject models outside the active runtime's zero-cost allowlist."""
        allowed = (
            LOCAL_ALLOWED_AI_MODELS
            if self.runtime is RuntimeMode.LOCAL
            else ALLOWED_AI_MODELS
        )
        for model in (self.embedding_model, self.generation_model):
            if model not in allowed:
                message = f"Model not allowed under the zero-cost policy: {model}"
                raise ValueError(message)
        if not self.embedding_model.strip() or not self.generation_model.strip():
            message = "model names must not be empty"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_runtime_values(self) -> "Settings":
        """Reject invalid local runtime and AI budget settings."""
        for name in ("direct_qa_threshold", "synthesis_threshold"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                message = f"{name} must be between 0 and 1, got {value!r}"
                raise ValueError(message)
        for name in ("qa_top_k", "message_top_k"):
            if getattr(self, name) < 1:
                message = f"{name} must be at least 1"
                raise ValueError(message)
        if self.recap_interval_hours <= 0 or self.admin_report_interval_min <= 0:
            message = "report intervals must be greater than zero"
            raise ValueError(message)
        if self.ai_daily_neuron_budget <= 0:
            message = "AI daily budget must be greater than zero"
            raise ValueError(message)
        if self.runtime is RuntimeMode.CLOUDFLARE and self.log_content:
            message = "KB_LOG_CONTENT cannot be enabled in Cloudflare mode"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_classifier_thresholds(self) -> "Settings":
        """Reject classifier thresholds outside the similarity range."""
        for name in (
            "classifier_chitchat_discard_threshold",
            "classifier_keep_signal_threshold",
            "classifier_question_match_threshold",
            "classifier_answer_match_threshold",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                message = f"{name} must be between 0 and 1, got {value!r}"
                raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_budget_fractions(self) -> "Settings":
        """Keep background and maintenance budget ceilings ordered."""
        background = self.ai_background_budget_fraction
        maintenance = self.ai_maintenance_budget_fraction
        if not 0.0 < background < maintenance < 1.0:
            message = (
                "AI background/maintenance fractions must satisfy "
                "0 < background < maintenance < 1"
            )
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_admin_report_mode(self) -> "Settings":
        """Reject unknown admin report modes."""
        if self.admin_report_mode not in {"always", "batch", "off"}:
            message = (
                "ADMIN_REPORT_MODE must be 'always', 'batch' or 'off', got"
                f" {self.admin_report_mode!r}"
            )
            raise ValueError(message)
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
