# SPDX-License-Identifier: MIT

"""Internal HTTP fixture operations for the E2E harness.

Only deterministic fixture setup/cleanup and the daily-report operator flow
use these routes: group binding, sentinel seeding, revert-based reset, and the
daily report. The answer path itself is always driven through real Telegram;
the harness never injects webhook updates or calls eval routes.
"""

from typing import Any

import httpx

from e2e.telegram.client import E2ERuntimeError
from e2e.telegram.config import (
    BASELINE_ANSWER,
    COLLAPSE_ANSWER,
    COLLAPSE_QUESTION,
    MAX_REVERTS_PER_ITEM,
    SCOPE_GLOBAL,
    SENTINEL_QUESTION,
    SOURCE_ANCHOR,
    SOURCE_URL,
    SPACE_A,
    TelegramE2ESettings,
)
from knowledge_bot.application.seed import stable_id
from knowledge_bot.domain.identity import canonical_key_for


def qa_item_id_for(question: str, scope: str) -> str:
    """Derive the seeded Q&A item id the way the repository does.

    Args:
        question: The canonical question.
        scope: The scope the entry was seeded into.

    Returns:
        The deterministic Q&A item id (`qa-<hash>` plus scope suffix).
    """
    key = canonical_key_for(question)
    if scope == SCOPE_GLOBAL:
        return f"qa-{stable_id(key)}"
    return f"qa-{stable_id(key)}:{scope}"


def _collapse_entry(run_id: str) -> dict[str, Any]:
    """Build one seed entry for the run-scoped collapsing question.

    Returns:
        A JSON-ready `SeedQA`-shaped dict.
    """
    entry = _sentinel_entry()
    entry["question"] = COLLAPSE_QUESTION
    entry["answer"] = COLLAPSE_ANSWER.format(run_id=run_id)
    return entry


def _sentinel_entry() -> dict[str, Any]:
    """Build one seed entry for the fixed sentinel question.

    Returns:
        A JSON-ready `SeedQA`-shaped dict.
    """
    return {
        "source_url": SOURCE_URL,
        "source_kind": "web_seed",
        "source_authority": 90,
        "section": "e2e",
        "question": SENTINEL_QUESTION,
        "answer": BASELINE_ANSWER,
        "status": "published",
        "retrieved_at": "2025-01-01T00:00:00Z",
        "source_anchor": SOURCE_ANCHOR,
    }


class InternalWorker:
    """Small httpx client for the deployed Worker's internal routes.

    Args:
        settings: Validated E2E settings.

    Attributes:
        settings: The validated settings.
    """

    def __init__(self, settings: TelegramE2ESettings) -> None:
        """Create the async client without opening a connection."""
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            headers=settings.internal_headers,
            timeout=120.0,
        )

    async def close(self) -> None:
        """Close the HTTP connection pool."""
        await self._http.aclose()

    async def register_group(
        self,
        chat_id: str,
        title: str,
        space_id: str,
        *,
        bot_mode: str | None = None,
    ) -> str:
        """Bind a dedicated E2E group into its fixed logical space.

        Args:
            chat_id: The Bot-API-compatible signed chat id.
            title: The exact group title (must start with `[E2E]`).
            space_id: The fixed logical space id.
            bot_mode: Optional mode to set at the same time; omitting it keeps
                whatever the group already had.

        Returns:
            The space id the Worker reports back.

        Raises:
            E2ERuntimeError: On a non-success response.
        """
        response = await self._http.post(
            "/internal/groups",
            json={
                "chat_id": chat_id,
                "title": title,
                "space_id": space_id,
                "bot_mode": bot_mode,
            },
        )
        if response.status_code != 200:
            raise E2ERuntimeError("group_bind_failed", str(response.status_code))
        return str(response.json()["space_id"])

    async def seed_baseline(self, scope: str) -> None:
        """Seed the fixed sentinel question into one scope, renewing it.

        Args:
            scope: `global` or `space:<space_id>`.

        Raises:
            E2ERuntimeError: On failure or a missing projection.
        """
        response = await self._http.post(
            "/internal/seed",
            json={"qa": [_sentinel_entry()], "scope": scope, "renew": True},
        )
        if response.status_code != 200:
            raise E2ERuntimeError(
                "seed_failed", f"{scope}: HTTP {response.status_code}"
            )
        body = response.json()
        # After a reset the item may already sit at the baseline version, in
        # which case renewing it is a no-op with nothing new to project.
        if body.get("indexed", 0) not in (0, 1):
            detail = f"sentinel projection unexpected in {scope}: {body}"
            raise E2ERuntimeError("seed_not_indexed", detail)

    async def seed_collapse_question(self, run_id: str) -> None:
        """Seed the run-scoped collapsing question into global knowledge only.

        Args:
            run_id: The run token, so every run reuses the same question and a
                different answer.

        Raises:
            E2ERuntimeError: On failure or a missing projection.
        """
        response = await self._http.post(
            "/internal/seed",
            json={
                "qa": [_collapse_entry(run_id)],
                "scope": SCOPE_GLOBAL,
                "renew": True,
            },
        )
        if response.status_code != 200:
            raise E2ERuntimeError(
                "seed_failed", f"collapse question: HTTP {response.status_code}"
            )
        body = response.json()
        if body.get("indexed", 0) not in (0, 1):
            detail = f"collapse projection unexpected: {body}"
            raise E2ERuntimeError("seed_not_indexed", detail)

    async def reset_collapse_question(self) -> None:
        """Revert the collapsing question so the next run starts from nothing."""
        await self.reset_item(qa_item_id_for(COLLAPSE_QUESTION, SCOPE_GLOBAL))

    async def revert_item(self, qa_item_id: str) -> bool:
        """Revert one Q&A item one version; `False` when nothing is left.

        Args:
            qa_item_id: The deterministic item id to revert.

        Returns:
            True when a version was reverted, False on `404`.

        Raises:
            E2ERuntimeError: On any other failure.
        """
        response = await self._http.post(
            "/internal/revert", json={"qa_item_id": qa_item_id}
        )
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise E2ERuntimeError(
                "revert_failed", f"{qa_item_id}: HTTP {response.status_code}"
            )
        return True

    async def reset_item(self, qa_item_id: str) -> None:
        """Revert one item repeatedly until `404` or the plan's cap.

        Args:
            qa_item_id: The deterministic item id to reset.

        Raises:
            E2ERuntimeError: If the cap is reached while versions remain.
        """
        for _ in range(MAX_REVERTS_PER_ITEM):
            if not await self.revert_item(qa_item_id):
                return
        raise E2ERuntimeError("revert_cap_reached", qa_item_id)

    async def run_daily_report(self) -> None:
        """Force the daily report job and require status `sent`.

        Raises:
            E2ERuntimeError: On failure or a non-`sent` status.
        """
        response = await self._http.post(
            "/internal/jobs/daily-report", json={"force": True}
        )
        if response.status_code != 200:
            raise E2ERuntimeError("daily_report_failed", str(response.status_code))
        status = response.json().get("status")
        if status != "sent":
            raise E2ERuntimeError("daily_report_skipped", f"status={status!r}")


async def reset_and_seed_baseline(settings: TelegramE2ESettings) -> None:
    """Reset the two dedicated sentinel items, then seed the baseline.

    Args:
        settings: Validated E2E settings.

    Raises:
        E2ERuntimeError: Propagated from the internal client.
    """
    worker = InternalWorker(settings)
    try:
        await worker.reset_item(qa_item_id_for(SENTINEL_QUESTION, SCOPE_GLOBAL))
        scope_a = f"space:{SPACE_A}"
        await worker.reset_item(qa_item_id_for(SENTINEL_QUESTION, scope_a))
        await worker.seed_baseline(SCOPE_GLOBAL)
        await worker.seed_baseline(scope_a)
    finally:
        await worker.close()
