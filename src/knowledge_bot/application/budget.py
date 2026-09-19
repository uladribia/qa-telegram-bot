# SPDX-License-Identifier: MIT
"""Estimated Workers AI spend, and the guard that protects the daily quota.

The free plan gives 10,000 neurons per day. When that runs out every model call
fails and the bot can answer nothing until midnight UTC, so a single careless
eval run can take the whole bot down for a day.

Cloudflare does not expose the real usage to a Worker, so calls are metered with
a character-based estimate. The estimate exists to make the guard *early* rather
than exact: a deliberate reserve is held back for real user traffic, and only
the expensive admin paths (evals and reindex) consult it. A user question is
never refused by this guard — it degrades to the temporary-unavailable reply if
the quota really is gone.
"""

from dataclasses import dataclass

from knowledge_bot.ports.budget import AiUsageRepository
from knowledge_bot.ports.clock import Clock

# Characters per token, for the estimate. Rough, and only ever used as a ratio.
_CHARS_PER_TOKEN = 4.0


@dataclass(frozen=True, slots=True)
class AiSpend:
    """What has been spent today, and what is left for each kind of work."""

    neurons: float
    limit: float
    evaluation_ceiling: float

    @property
    def exhausted(self) -> bool:
        """True when even user traffic should stop spending."""
        return self.neurons >= self.limit

    @property
    def evaluation_allowed(self) -> bool:
        """True when there is room for an eval or reindex run."""
        return self.neurons < self.evaluation_ceiling


@dataclass(frozen=True, slots=True)
class AiBudget:
    """Meter estimated model spend and decide what may still run."""

    usage: AiUsageRepository
    clock: Clock
    daily_neurons: float = 10_000.0
    reserve_fraction: float = 0.25
    embed_neurons_per_char: float = 0.015
    chat_neurons_per_char: float = 0.020

    def day(self) -> str:
        """Return the current UTC day key."""
        return self.clock.now().strftime("%Y-%m-%d")

    def _ceiling(self) -> float:
        reserve = min(max(self.reserve_fraction, 0.0), 0.95)
        return self.daily_neurons * (1.0 - reserve)

    async def spend(self) -> AiSpend:
        """Return today's spend and the remaining room."""
        neurons, _ = await self.usage.get(self.day())
        return AiSpend(
            neurons=neurons,
            limit=self.daily_neurons,
            evaluation_ceiling=self._ceiling(),
        )

    def estimate_embedding(self, characters: int) -> float:
        """Estimate the neurons an embedding call costs."""
        return max(characters, 0) * self.embed_neurons_per_char

    def estimate_chat(self, prompt: str, response: str) -> float:
        """Estimate the neurons a chat call costs, counting both directions."""
        characters = len(prompt) + len(response)
        return max(characters, 0) * self.chat_neurons_per_char

    async def record(self, neurons: float) -> None:
        """Add an estimate to today's total."""
        day = self.day()
        await self.usage.add(day, neurons, 1)

    async def record_embedding(self, texts: list[str]) -> None:
        """Meter an embedding call over the given texts."""
        await self.record(self.estimate_embedding(sum(len(text) for text in texts)))

    async def record_chat(self, prompt: str, response: str) -> None:
        """Meter a chat call over its prompt and response."""
        await self.record(self.estimate_chat(prompt, response))

    async def evaluation_allowed(self) -> bool:
        """Return whether an expensive admin run may start."""
        return (await self.spend()).evaluation_allowed


def tokens_of(characters: int) -> float:
    """Return a token estimate for a character count."""
    return max(characters, 0) / _CHARS_PER_TOKEN
