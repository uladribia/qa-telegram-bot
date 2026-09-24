# SPDX-License-Identifier: MIT
"""Build and send the deterministic daily operations report."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import (
    DailyReportSnapshot,
    DailyReportSource,
    DailyReportStateRepository,
)
from knowledge_bot.ports.transport import MessageTransport


@dataclass(frozen=True, slots=True)
class DailyReportService:
    """Send one deterministic report for a completed daily window."""

    source: DailyReportSource
    state: DailyReportStateRepository
    transport: MessageTransport
    budget: AiBudget
    clock: Clock
    admin_principal_id: str

    async def run(self, *, force: bool = False) -> bool:
        """Send the report when due and persist only successful delivery."""
        now = self.clock.now()
        last_sent = await self.state.get("admin")
        window_start = last_sent or now - timedelta(hours=24)
        if (
            not force
            and last_sent is not None
            and now - last_sent < timedelta(hours=24)
        ):
            return False
        snapshot = await self.source.collect(window_start, now)
        neurons, calls = await self.budget.usage_today()
        text = self._render(snapshot, neurons, calls, window_start, now)
        sent = await self.transport.send_message(self.admin_principal_id, text)
        if sent is None:
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
        """Render the fixed report sections without raw knowledge text."""
        lines = [
            f"📊 Resum diari ({start} - {end})",
            "",
            "Preguntes adreçades: "
            f"{snapshot.addressed_total} total, {snapshot.direct} directes, "
            f"{snapshot.synthesis} síntesi, {snapshot.abstention} abstencions, "
            f"{snapshot.unavailable} no disponibles, {snapshot.flagged} marcades.",
            "Preguntes de fons: "
            f"{snapshot.background_questions} detectades, "
            f"{snapshot.background_paired} amb resposta aparellada.",
            "Ingesta de fons: "
            f"{snapshot.messages_stored} missatges, "
            f"{snapshot.evidence_indexed} evidències indexades, "
            f"{snapshot.non_evidence} no-evidència, "
            f"{snapshot.deferred} ajornades, {snapshot.failures} fallides.",
            "Correccions: "
            f"{snapshot.corrections_proposed} propostes, "
            f"{snapshot.approved_local} locals, "
            f"{snapshot.approved_global} globals, "
            f"{snapshot.rejected} rebutjades.",
            f"Divergència de seeds: {snapshot.seed_divergences}.",
            f"IA: {calls} crides, {neurons:.0f} neurones estimades, "
            f"pressupost {self.budget.daily_neurons:.0f}.",
        ]
        if snapshot.audit_labels:
            lines.extend(
                [
                    "Auditoria revisors:",
                    *[f"- {label}" for label in snapshot.audit_labels],
                ]
            )
        return "\n".join(lines)
