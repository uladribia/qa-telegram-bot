# SPDX-License-Identifier: MIT
"""Tests for the estimated AI spend meter and its guard."""

from datetime import UTC, datetime

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.domain.enums import AiWorkClass
from tests.fakes.support import FrozenClock, InMemoryAiUsageRepository

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def _budget(usage: InMemoryAiUsageRepository | None = None) -> AiBudget:
    return AiBudget(
        usage=usage if usage is not None else InMemoryAiUsageRepository(),
        clock=FrozenClock(NOW),
        daily_neurons=1000.0,
        reserve_fraction=0.25,
        embed_neurons_per_char=0.01,
        chat_neurons_per_char=0.02,
    )


async def test_a_fresh_day_allows_evaluations() -> None:
    """With nothing spent, an expensive admin run is allowed."""
    budget = _budget()
    assert await budget.evaluation_allowed() is True
    spend = await budget.spend()
    assert spend.neurons == 0.0
    assert spend.limit == 1000.0
    assert spend.evaluation_ceiling == 750.0


async def test_spend_is_recorded_against_the_utc_day() -> None:
    """Estimates accumulate for today's key."""
    usage = InMemoryAiUsageRepository()
    budget = _budget(usage)
    await budget.record_embedding(["abcd", "efgh"])
    await budget.record_chat("a" * 100, "b" * 100)
    spend = await budget.spend()
    assert spend.neurons == 0.01 * 8 + 0.02 * 200
    _, calls = await usage.get("2026-09-19")
    assert calls == 2


async def test_evaluations_are_refused_once_the_reserve_is_reached() -> None:
    """The reserve is held back for real user traffic."""
    usage = InMemoryAiUsageRepository()
    await usage.add("2026-09-19", 700.0, 1)
    budget = _budget(usage)
    assert await budget.evaluation_allowed() is True
    await usage.add("2026-09-19", 60.0, 1)
    assert await budget.evaluation_allowed() is False
    # Still short of the hard limit: user questions may keep spending.
    assert (await budget.spend()).exhausted is False


async def test_background_work_stops_at_its_lower_budget_ceiling() -> None:
    """User work remains admissible after background work is refused."""
    usage = InMemoryAiUsageRepository()
    await usage.add("2026-09-19", 510.0, 1)
    budget = _budget(usage)
    assert await budget.work_allowed(AiWorkClass.BACKGROUND) is False
    assert await budget.work_allowed(AiWorkClass.MAINTENANCE) is True
    assert await budget.work_allowed(AiWorkClass.USER) is True


async def test_the_hard_limit_stops_everything() -> None:
    """Past the hard limit the day is exhausted."""
    usage = InMemoryAiUsageRepository()
    await usage.add("2026-09-19", 1000.0, 1)
    spend = await _budget(usage).spend()
    assert spend.exhausted is True
    assert spend.evaluation_allowed is False
