# SPDX-License-Identifier: MIT
"""Build and send the deterministic daily operations report."""

from dataclasses import dataclass
from datetime import timedelta

from knowledge_bot.application.recap_service import RecapService
from knowledge_bot.application.reviewers import ReviewerReportService
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import DailyReportStateRepository
from knowledge_bot.ports.transport import MessageTransport


@dataclass(frozen=True, slots=True)
class DailyReportService:
    """Run recap and reviewer reporting once per daily window."""

    recap: RecapService
    reviewer_report: ReviewerReportService
    state: DailyReportStateRepository
    transport: MessageTransport
    clock: Clock
    admin_principal_id: str

    async def run(self, *, force: bool = False) -> bool:
        """Send due deterministic reports and record successful completion."""
        now = self.clock.now()
        last_sent = await self.state.get("admin")
        if (
            not force
            and last_sent is not None
            and now - last_sent < timedelta(hours=24)
        ):
            return False
        recap_sent = await self.recap.maybe_send()
        review_sent = await self.reviewer_report.maybe_send()
        if not recap_sent and not review_sent:
            sent_id = await self.transport.send_message(
                self.admin_principal_id,
                "📊 Resum diari\n\nNo hi ha novetats pendents per reportar.",
            )
            if sent_id is None:
                return False
        await self.state.set("admin", now)
        return True
