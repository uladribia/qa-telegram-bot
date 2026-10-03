# SPDX-License-Identifier: MIT
"""Application settings loaded from the environment."""

# ruff: noqa: TRY003

from enum import StrEnum
from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from knowledge_bot.domain.enums import BotMode

ALLOWED_AI_MODELS = frozenset(
    {
        "@cf/google/embeddinggemma-300m",
        "@cf/mistralai/mistral-small-3.1-24b-instruct",
        "@cf/zai-org/glm-4.7-flash",
    }
)
LOCAL_ALLOWED_AI_MODELS = frozenset({"embeddinggemma", "gemma3:270m"})


class RuntimeMode(StrEnum):
    """Supported application runtimes."""

    LOCAL = "local"
    CLOUDFLARE = "cloudflare"


class DecisionBackend(StrEnum):
    """Which model decides what a listener message is and what it answers.

    ``baseline`` is the linear classifier plus deterministic pairing.
    ``systemone`` sends one request per message to a local decision service.
    """

    BASELINE = "baseline"
    SYSTEM_ONE = "systemone"


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

    # Telemetry master switch. The write token itself is not a setting: the
    # SDK reads LOGFIRE_TOKEN or the local project credentials.
    logfire_send_to_logfire: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "KB_LOGFIRE_SEND_TO_LOGFIRE",
        ),
    )
    # Export message text, sender identity, prompts, and answers. On while
    # testing so a flow can be reconstructed; credentials stay scrubbed.
    logfire_capture_content: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "KB_LOGFIRE_CAPTURE_CONTENT",
        ),
    )
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_bot_id: str = ""
    telegram_bot_username: str = ""
    allowed_telegram_user_ids: str = ""
    admin_telegram_user_id: str = ""
    #: How the bot behaves in a private chat. A private message always
    #: addresses the bot, so ``proactive`` is the same as ``active`` here; the
    #: mode exists to turn the DM conversation off or mute it without touching
    #: the groups.
    telegram_dm_bot_mode: BotMode = BotMode.ACTIVE
    internal_admin_key: str = ""

    embedding_model: str = "@cf/google/embeddinggemma-300m"
    generation_model: str = "@cf/mistralai/mistral-small-3.1-24b-instruct"
    ai_embed_timeout_seconds: float = Field(default=10.0, gt=0, le=55)
    ai_generation_timeout_seconds: float = Field(default=35.0, gt=0, le=55)
    ai_generation_max_tokens: int = Field(default=1024, gt=0)
    # Qwen3 thinks by default and the deliberation does not fit the adapter
    # deadline. TEMPORARY, for the production model comparison only.
    ai_append_no_think: bool = Field(
        default=False, validation_alias=AliasChoices("AI_APPEND_NO_THINK")
    )
    # GLM-4.5 and later think by default and the deliberation does not fit the
    # adapter deadline. Sent as chat_template_kwargs.enable_thinking.
    ai_disable_thinking: bool = Field(
        default=False, validation_alias=AliasChoices("AI_DISABLE_THINKING")
    )

    reviewer_escalation_timeout_seconds: int = 86_400
    pairing_question_window_minutes: int = 5
    pairing_max_pending_questions: int = 5
    #: Which backend decides a listener message's intent and pair relevance.
    #: ``baseline`` is the linear classifier plus deterministic pairing and
    #: stays the default; ``systemone`` asks one decision service per message.
    decision_backend: DecisionBackend = DecisionBackend.BASELINE
    decision_base_url: str = "http://knowledge-bot-ollama:11434"
    decision_model: str = "tev1:0.8b"
    ai_decision_timeout_seconds: float = Field(default=20.0, gt=0, le=55)
    #: Ask the decision service for candidate relevance as well as intent.
    #: Off by default: the model then decides what a message *is*, and the
    #: deterministic policy still decides which question it answers. Turning
    #: it on sends one request carrying every candidate relevance and lets the
    #: model's relevance scores accept at most one pair.
    decision_include_relevance: bool = False
    #: Fall back to the baseline decision when the service is unreachable or
    #: answers something unusable. On, so a decision-service outage degrades
    #: the listener instead of losing the message; every fallback is logged.
    decision_fallback_to_baseline: bool = True
    retroeval_relevance_threshold: float = 0.80
    retroeval_relevance_margin: float = 0.15
    classifier_confidence_threshold: float = 0.60
    classifier_margin_threshold: float = 0.15
    classifier_model_path: str = "data/classifier/model.json"
    answer_similarity_floor: float = 0.35
    qa_top_k: int = 5
    message_top_k: int = 2
    # Lexical leg over Q&A answer text. Its own width, because BM25 scores are
    # unbounded and not comparable with the cosine floor above. Zero disables
    # the leg: it measured neutral-to-negative on the live gold set, so shipping
    # it is an explicit decision, not the default. See docs/operations.md.
    qa_answer_top_k: int = 0
    # Minimum BM25 strength. SQLite's bm25 zeroes a term's contribution as its
    # document frequency rises, so a near-zero total means the query matched
    # only ubiquitous words and nothing discriminative.
    qa_answer_min_strength: float = 0.5
    # Keep hits scoring at least this fraction of the query's own best hit.
    qa_answer_relative_cut: float = 0.5
    # Authority given to a lexical hit. Provenance stays official: the text is
    # the club's own. Only the match is unverified, and rule 4 of the prompt
    # keeps authority from ever licensing an answer.
    lexical_authority: int = 45
    ai_daily_neuron_budget: float = 10_000.0
    ai_neuron_reserve_fraction: float = 0.25
    ai_background_budget_fraction: float = 0.50
    ai_maintenance_budget_fraction: float = 0.70
    #: Uninvited answers stop here, below the background class: they are the
    #: first thing to go, and never at the cost of the classification and
    #: indexing that make tomorrow's answers possible.
    ai_proactive_budget_fraction: float = 0.35
    ai_embed_neurons_per_char: float = 0.015
    ai_chat_neurons_per_char: float = 0.020

    @model_validator(mode="after")
    def _validate_allowed_models(self) -> "Settings":
        """Reject models outside the active runtime allowlist."""
        allowed = (
            LOCAL_ALLOWED_AI_MODELS
            if self.runtime is RuntimeMode.LOCAL
            else ALLOWED_AI_MODELS
        )
        for model in (self.embedding_model, self.generation_model):
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
            "retroeval_relevance_threshold",
            "retroeval_relevance_margin",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.qa_top_k < 1 or self.message_top_k < 1:
            message = "qa_top_k and message_top_k must be at least 1"
            raise ValueError(message)
        if self.qa_answer_top_k < 0:
            message = "qa_answer_top_k must not be negative"
            raise ValueError(message)
        if not 0.0 <= self.qa_answer_relative_cut <= 1.0:
            message = "qa_answer_relative_cut must be between 0 and 1"
            raise ValueError(message)
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
            < self.ai_proactive_budget_fraction
            < self.ai_background_budget_fraction
            < self.ai_maintenance_budget_fraction
            < 1
        ):
            raise ValueError("budget fractions must be ordered")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
