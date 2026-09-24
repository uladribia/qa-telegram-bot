# SPDX-License-Identifier: MIT
"""Build and send the deterministic daily operations report."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.notifier import Notifier
from knowledge_bot.ports.repositories import (
    DailyReportSnapshot,
    DailyReportSource,
    DailyReportStateRepository,
)


@dataclass(frozen=True, slots=True)
class DailyReportService:
    """Send one deterministic report for a completed daily window."""

    source: DailyReportSource
    state: DailyReportStateRepository
    notifier: Notifier
    budget: AiBudget
    clock: Clock
    admin_principal_id: str

    async def preview(self) -> str:
        """Render the current report without sending or changing state."""
        now = self.clock.now()
        start = await self.state.get("admin") or now - timedelta(hours=24)
        snapshot = await self.source.collect(start, now)
        neurons, calls = await self.budget.usage_today()
        return self._render(snapshot, neurons, calls, start, now)

    async def run(self, *, force: bool = False, dry_run: bool = False) -> bool:
        """Send the report when due and persist only successful delivery."""
        if dry_run:
            return False
        now = self.clock.now()
        last_sent = await self.state.get("admin")
        start = last_sent or now - timedelta(hours=24)
        if (
            not force
            and last_sent is not None
            and now - last_sent < timedelta(hours=24)
        ):
            return False
        snapshot = await self.source.collect(start, now)
        neurons, calls = await self.budget.usage_today()
        text = self._render(snapshot, neurons, calls, start, now)
        if not await self.notifier.send_text(self.admin_principal_id, text):
            return False
        await self.state.set("admin", now)
        return True

    def _render(
        self,
        snapshot: DailyReportSnapshot,
        neurons: float,
        calls: int,
        start: datetime,
        end: datetime,
    ) -> str:
        """Render deterministic report sections without private content."""
        return "\n".join(
            [
                f"📊 Resum diari ({start} - {end})",
                "",
                (
                    f"Preguntes adreçades: {snapshot.addressed_total} total, "
                    f"{snapshot.direct} directes, {snapshot.synthesis} síntesi, "
                    f"{snapshot.abstention} abstencions, "
                    f"{snapshot.unavailable} no disponibles, "
                    f"{snapshot.flagged} marcades."
                ),
                (
                    f"Preguntes de fons: {snapshot.background_questions} detectades, "
                    f"{snapshot.background_paired} amb resposta aparellada."
                ),
                (
                    f"Ingesta de fons: {snapshot.messages_stored} missatges, "
                    f"{snapshot.evidence_indexed} evidències indexades, "
                    f"{snapshot.non_evidence} no-evidència, "
                    f"{snapshot.deferred} ajornades, "
                    f"{snapshot.failures} fallides."
                ),
                (
                    f"Correccions: {snapshot.corrections_proposed} propostes, "
                    f"{snapshot.approved_local} locals, "
                    f"{snapshot.approved_global} globals, "
                    f"{snapshot.rejected} rebutjades."
                ),
                f"Divergència de seeds: {snapshot.seed_divergences}.",
                f"Projeccions: {snapshot.projection_pending} pendents, "
                f"{snapshot.projection_failed} fallides.",
                f"IA: {calls} crides, {neurons:.0f} neurones estimades, "
                f"pressupost {self.budget.daily_neurons:.0f}.",
            ]
        )
