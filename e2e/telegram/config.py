# SPDX-License-Identifier: MIT

"""Settings and constants for the real Telegram E2E run.

Configuration comes from `.env.e2e` (never committed). Nothing here touches
`knowledge_bot.infrastructure.settings`: the E2E harness is an external actor
and keeps its own credential surface (Telethon API id/hash, internal admin
key, deployed Worker URL).
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The sentinel group titles must be explicitly dedicated E2E spaces.
E2E_TITLE_PREFIX = "[E2E]"

# Stable logical spaces the dedicated groups are bound into on every run.
SPACE_A = "sp_000000000000000000000000000000a1"
SPACE_B = "sp_000000000000000000000000000000b2"

SCOPE_A = f"space:{SPACE_A}"
SCOPE_GLOBAL = "global"

# Fixed canonical sentinel question for every run; run-scoped tokens make the
# answers distinguishable across runs without re-seeding new questions.
SENTINEL_QUESTION = "Quin és el codi de la prova E2E de Telegram?"
BASELINE_TOKEN = "E2E-BASELINE-42"
BASELINE_ANSWER = f"El codi de la prova E2E de Telegram és {BASELINE_TOKEN}."
SOURCE_URL = "https://e2e.invalid/telegram"
SOURCE_ANCHOR = "telegram-e2e-sentinel"

# Polling cadence when watching a chat for new bot messages.
POLL_INTERVAL_SECONDS = 0.75

# Quiet window proving an unaddressed human question gets no direct answer
# while background processing happens.
LISTENER_QUIET_SECONDS = 30

# Bounded wait after an explicit reply for listener classification, pairing
# and projection to complete before retrieval is asserted.
PROJECTION_WAIT_SECONDS = 40

# Window given to deferred webhook processing before checking for a reply.
DEFERRED_REPLY_GRACE_SECONDS = 15

# Revert cleanup never loops forever; the plan caps it explicitly.
MAX_REVERTS_PER_ITEM = 10


class TelegramE2ESettings(BaseSettings):
    """Settings for the deployed-Worker Telegram E2E harness.

    Loaded from `.env.e2e` in the repository root. None of these keys belong to
    the application's own settings: they exist only for this harness.

    Attributes:
        allow_cloudflare_live_tests: Set to `1` to authorize the live run.
        bot_base_url: HTTPS base URL of the deployed Worker.
        internal_admin_key: The deployed Worker's internal admin key.
        telegram_e2e_api_id: Telegram API id of the existing human account.
        telegram_e2e_api_hash: Telegram API hash of the existing human account.
        telegram_e2e_bot_username: The bot's @username, without the @.
        telegram_e2e_group_a_title: Exact title of dedicated E2E group A.
        telegram_e2e_group_b_title: Exact title of dedicated E2E group B.
        telegram_e2e_session_path: File path for the Telethon session file.
        telegram_e2e_timeout_seconds: Default wait timeout for bot messages.
    """

    model_config = SettingsConfigDict(
        env_file=".env.e2e",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    allow_cloudflare_live_tests: int = 0
    bot_base_url: str = ""
    internal_admin_key: str = ""
    telegram_e2e_api_id: int = 0
    telegram_e2e_api_hash: str = ""
    telegram_e2e_bot_username: str = ""
    telegram_e2e_group_a_title: str = "[E2E] QA A"
    telegram_e2e_group_b_title: str = "[E2E] QA B"
    telegram_e2e_session_path: str = ".e2e/telegram-user"
    telegram_e2e_timeout_seconds: int = 75

    def validate_strict(self) -> None:
        """Raise `E2EConfigError` unless every precondition holds.

        Called before the client connects or mutates anything.

        Raises:
            E2EConfigError: On any missing or invalid setting.
        """
        problems: list[str] = []
        if self.allow_cloudflare_live_tests != 1:
            problems.append("ALLOW_CLOUDFLARE_LIVE_TESTS must be 1")
        base = self.bot_base_url.rstrip("/")
        if not base.startswith("https://"):
            problems.append("BOT_BASE_URL must be a non-empty HTTPS URL")
        if not self.internal_admin_key:
            problems.append("INTERNAL_ADMIN_KEY is required")
        if self.telegram_e2e_api_id <= 0:
            problems.append("TELEGRAM_E2E_API_ID is required")
        if not self.telegram_e2e_api_hash:
            problems.append("TELEGRAM_E2E_API_HASH is required")
        if not self.telegram_e2e_bot_username:
            problems.append("TELEGRAM_E2E_BOT_USERNAME is required")
        title_a = self.telegram_e2e_group_a_title
        title_b = self.telegram_e2e_group_b_title
        if not title_a.startswith(E2E_TITLE_PREFIX):
            problems.append(
                f"TELEGRAM_E2E_GROUP_A_TITLE must start with {E2E_TITLE_PREFIX}"
            )
        if not title_b.startswith(E2E_TITLE_PREFIX):
            problems.append(
                f"TELEGRAM_E2E_GROUP_B_TITLE must start with {E2E_TITLE_PREFIX}"
            )
        if title_a == title_b:
            problems.append("E2E group A and B titles must differ")
        if self.telegram_e2e_timeout_seconds <= 0:
            problems.append("TELEGRAM_E2E_TIMEOUT_SECONDS must be positive")
        if problems:
            raise E2EConfigError("; ".join(problems))

    @property
    def base_url(self) -> str:
        """Return the deployed Worker base URL without a trailing slash."""
        return self.bot_base_url.rstrip("/")

    @property
    def timeout_seconds(self) -> int:
        """Return the default timeout for bot-message waits."""
        return self.telegram_e2e_timeout_seconds

    @property
    def session_path(self) -> Path:
        """Return the Telethon session file path as a `Path`."""
        return Path(self.telegram_e2e_session_path)

    @property
    def internal_headers(self) -> dict[str, str]:
        """Return the HTTP headers for internal Worker routes."""
        return {"X-Internal-Key": self.internal_admin_key}


class E2EConfigError(RuntimeError):
    """Raised when the E2E configuration is missing or invalid."""
