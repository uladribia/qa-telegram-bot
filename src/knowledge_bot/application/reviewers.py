# SPDX-License-Identifier: MIT
"""Reviewer nomination, routing, and the admin report on their resolutions.

There is exactly one reviewer per scope: each registered group may have its
own, plus one global. A correction proposal goes to the reviewer of the group
it came from, else the global reviewer, else the admin (the env fallback, who
keeps power over everything). Confirmations are checked server-side: only the
assigned reviewer, the global reviewer, or the admin may approve, edit, or
reject. Only the admin may nominate or remove reviewers, and only via the
``/reviewer`` Telegram command.
"""

from dataclasses import dataclass

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.domain.entities import Reviewer, ReviewerEvent
from knowledge_bot.domain.scope import GLOBAL_SCOPE
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import (
    ReportStateRepository,
    ReviewerEventRepository,
    ReviewerRepository,
)
from knowledge_bot.ports.transport import MessageTransport

REPORT_HEADER = "\U0001f4cb Correccions revisades ({count}):"
REPORT_SPEND = (
    "\n\U0001f916 Consum d'IA avui: {neurons:.0f} / {limit:.0f} neurones "
    "en {calls} crides (estimat)."
)

_ACTION_LABELS: dict[str, str] = {
    "approved": "aprovada",
    "edited_approved": "editada i aprovada",
    "rejected": "rebutjada",
}


def parse_reviewer_command(text: str) -> tuple[str, bool]:
    """Parse a ``/reviewer`` command into its action and target scope.

    Args:
        text: The raw message text, starting with ``/reviewer``.

    Returns:
        ``(action, is_global)`` where action is ``nominate`` or ``remove``.
        The caller decides the scope: with ``global`` it is the global scope;
        otherwise the group the command was typed in. A plain ``/reviewer``
        (or ``/reviewer global``) with no ``off`` nominates; listing is what
        the caller does when there is no message to nominate from.
    """
    tokens = text.split()[1:]
    return ("remove" if "off" in tokens else "nominate"), "global" in tokens


def render_reviewer_list(reviewers: list[Reviewer]) -> str:
    """Render the current reviewers as a short text block.

    Args:
        reviewers: All reviewers, global scope first.

    Returns:
        A Catalan summary of who reviews which scope.
    """
    if not reviewers:
        return "No hi ha cap revisor nominat. Les correccions arriben a l'admin."
    labels = [
        f"\u2022 {'Global' if reviewer.scope == GLOBAL_SCOPE else reviewer.scope}"
        f" \u2192 {reviewer.name}"
        for reviewer in reviewers
    ]
    return "Revisors:\n" + "\n".join(labels)


def render_report(
    events: list[ReviewerEvent],
    *,
    spend: tuple[float, float, int] | None = None,
) -> str:
    """Render the admin report over resolved corrections.

    Args:
        events: The resolved corrections to report, ordered.
        spend: Optional ``(neurons, limit, calls)`` estimated AI usage of the
            day, appended as a final line when given.

    Returns:
        A read-only Catalan summary; the admin cannot act on it from here.
    """
    lines = [REPORT_HEADER.format(count=len(events))]
    for event in events:
        action = _ACTION_LABELS.get(event.action, event.action)
        scope = f" ({'global' if event.approval_scope == GLOBAL_SCOPE else 'grup'})"
        target = scope if event.action != "rejected" else ""
        lines.append(
            f"\u2022 {event.reviewer_name or '?'} \u00b7 "
            f'{event.group_label or "?"} \u00b7 "{event.question or "?"}"\n'
            f"  {action}{target} \u00b7 {event.created_at:%d/%m %H:%M}"
        )
    if spend is not None:
        neurons, limit, calls = spend
        lines.append(REPORT_SPEND.format(neurons=neurons, limit=limit, calls=calls))
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ReviewerRouter:
    """Resolve who reviews a correction and who may confirm it.

    The chain is: the reviewer of the group the correction came from, then the
    global reviewer, then the admin. The admin may always confirm.
    """

    reviewers: ReviewerRepository
    admin_user_id: str

    async def destination(self, group_chat_id: str | None) -> str | None:
        """Return the chat id that should review a correction from a group.

        Args:
            group_chat_id: The group the corrected answer came from.

        Returns:
            A chat id, or ``None`` when no admin is configured either.
        """
        if group_chat_id is not None:
            reviewer = await self.reviewers.get(group_chat_id)
            if reviewer is not None:
                return reviewer.user_id
        global_reviewer = await self.reviewers.get(GLOBAL_SCOPE)
        if global_reviewer is not None:
            return global_reviewer.user_id
        return self.admin_user_id or None

    async def can_confirm(self, user_id: str | None, group_chat_id: str | None) -> bool:
        """Return whether a user may confirm a correction from a group.

        Args:
            user_id: The raw Telegram user id of the person acting.
            group_chat_id: The group the corrected answer came from.

        Returns:
            ``True`` for the group's reviewer, the global reviewer, or the
            admin.
        """
        if user_id is None:
            return False
        if user_id == self.admin_user_id:
            return True
        if group_chat_id is not None:
            reviewer = await self.reviewers.get(group_chat_id)
            if reviewer is not None and reviewer.user_id == user_id:
                return True
        global_reviewer = await self.reviewers.get(GLOBAL_SCOPE)
        return global_reviewer is not None and global_reviewer.user_id == user_id


@dataclass(frozen=True, slots=True)
class ReviewerManager:
    """Nominate, list, and remove reviewers (admin-only, via Telegram)."""

    reviewers: ReviewerRepository
    clock: Clock

    async def nominate(
        self,
        scope: str,
        user_id: str,
        name: str,
        nominated_by: str,
    ) -> bool:
        """Create or replace the reviewer of a scope.

        Args:
            scope: ``global`` or the group chat id.
            user_id: The raw Telegram user id of the new reviewer.
            name: The reviewer's display name, for confirmations and reports.
            nominated_by: The admin's user id.

        Returns:
            ``True`` when a previous reviewer was replaced.
        """
        replaced = await self.reviewers.get(scope) is not None
        await self.reviewers.save(
            Reviewer(
                scope=scope,
                user_id=user_id,
                name=name,
                nominated_by=nominated_by,
                created_at=self.clock.now(),
            )
        )
        return replaced

    async def remove(self, scope: str) -> bool:
        """Remove the reviewer of a scope.

        Args:
            scope: ``global`` or the group chat id.

        Returns:
            ``True`` when a reviewer existed and was removed.
        """
        return await self.reviewers.delete(scope)

    async def list_reviewers(self) -> list[Reviewer]:
        """Return every reviewer, global scope first."""
        return await self.reviewers.all()


@dataclass(frozen=True, slots=True)
class ReviewerReportService:
    """Record reviewer resolutions and inform the admin about them.

    ``always`` sends one report per resolution, immediately. ``batch``
    consolidates everything pending once per interval, checked opportunistically
    on inbound events (Workers have no cron) or via the internal report poke.
    ``off`` records events but never reports them.
    """

    events: ReviewerEventRepository
    state: ReportStateRepository
    transport: MessageTransport
    clock: Clock
    admin_user_id: str
    mode: str = "always"
    interval_min: int = 60
    budget: AiBudget | None = None

    async def record(self, event: ReviewerEvent) -> None:
        """Record one reviewer resolution and report it if the mode says so.

        Args:
            event: The resolution to record.
        """
        stored = await self.events.add(event)
        if self.mode == "always" and self.admin_user_id:
            await self._send([stored])

    async def maybe_send(self) -> bool:
        """Send a consolidated report when the batch interval has elapsed.

        Returns:
            ``True`` when a report was sent.
        """
        if self.mode != "batch" or not self.admin_user_id:
            return False
        now = self.clock.now()
        last = await self.state.get_last_sent_at()
        if last is not None and (now - last).total_seconds() < self.interval_min * 60:
            return False
        pending = await self.events.list_unreported()
        if not pending:
            return False
        await self._send(pending)
        return True

    async def _spend(self) -> tuple[float, float, int] | None:
        """Return today's estimated ``(neurons, limit, calls)``, or ``None``."""
        if self.budget is None:
            return None
        neurons, calls = await self.budget.usage_today()
        return (neurons, self.budget.daily_neurons, calls)

    async def _send(self, events: list[ReviewerEvent]) -> None:
        await self.transport.send_message(
            self.admin_user_id, render_report(events, spend=await self._spend())
        )
        await self.events.mark_reported([event.feedback_id for event in events])
        await self.state.set_last_sent_at(self.clock.now())
