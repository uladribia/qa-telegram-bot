# SPDX-License-Identifier: MIT
"""Unit tests for reviewer nomination, routing, and the admin report."""

import asyncio
from datetime import UTC, datetime

import pytest

from knowledge_bot.application.reviewers import (
    ReviewerManager,
    ReviewerReportService,
    ReviewerRouter,
    parse_reviewer_command,
    render_report,
    render_reviewer_list,
)
from knowledge_bot.domain.entities import Reviewer, ReviewerEvent
from knowledge_bot.domain.scope import GLOBAL_SCOPE
from knowledge_bot.infrastructure.settings import Settings
from tests.fakes.repositories import (
    InMemoryReviewerEventRepository,
    InMemoryReviewerRepository,
)
from tests.fakes.support import FrozenClock, InMemoryReportStateRepository

NOW = datetime(2026, 9, 21, 18, 4, tzinfo=UTC)


def _manager() -> ReviewerManager:
    return ReviewerManager(
        reviewers=InMemoryReviewerRepository(), clock=FrozenClock(NOW)
    )


def test_parse_reviewer_command_variants() -> None:
    """The command parses into action and scope flag."""
    assert parse_reviewer_command("/reviewer") == ("nominate", False)
    assert parse_reviewer_command("/reviewer@bot") == ("nominate", False)
    assert parse_reviewer_command("/reviewer global") == ("nominate", True)
    assert parse_reviewer_command("/reviewer off") == ("remove", False)
    assert parse_reviewer_command("/reviewer off global") == ("remove", True)
    assert parse_reviewer_command("/reviewer ets el revisor") == ("nominate", False)


def test_nominate_upserts_and_remove_deletes() -> None:
    """Nominating replaces the scope's reviewer; removing clears it."""
    manager = _manager()
    assert asyncio.run(manager.nominate("-100", "222", "Pepe", "1")) is False
    assert asyncio.run(manager.nominate("-100", "333", "Marta", "1")) is True
    reviewer = asyncio.run(manager.reviewers.get("-100"))
    assert reviewer is not None and reviewer.user_id == "333"
    assert asyncio.run(manager.remove("-100")) is True
    assert asyncio.run(manager.remove("-100")) is False


def test_router_falls_back_from_group_to_global_to_admin() -> None:
    """The routing chain is group reviewer, then global reviewer, then admin."""
    router = ReviewerRouter(reviewers=InMemoryReviewerRepository(), admin_user_id="1")
    assert asyncio.run(router.destination("-100")) == "1"
    asyncio.run(router.reviewers.save(Reviewer(GLOBAL_SCOPE, "9", "G", NOW)))
    assert asyncio.run(router.destination("-100")) == "9"
    asyncio.run(router.reviewers.save(Reviewer("-100", "222", "Pepe", NOW)))
    assert asyncio.run(router.destination("-100")) == "222"
    assert asyncio.run(router.destination(None)) == "9"


def test_can_confirm_only_for_assigned_reviewers_and_admin() -> None:
    """Confirmation is limited to the group's reviewer, the global one, admin."""
    router = ReviewerRouter(reviewers=InMemoryReviewerRepository(), admin_user_id="1")
    asyncio.run(router.reviewers.save(Reviewer("-100", "222", "Pepe", NOW)))
    assert asyncio.run(router.can_confirm("222", "-100")) is True
    assert asyncio.run(router.can_confirm("222", "-200")) is False
    assert asyncio.run(router.can_confirm("1", "-200")) is True
    assert asyncio.run(router.can_confirm(None, "-100")) is False
    asyncio.run(router.reviewers.save(Reviewer(GLOBAL_SCOPE, "9", "G", NOW)))
    assert asyncio.run(router.can_confirm("9", "-200")) is True


def test_render_reviewer_list_and_report() -> None:
    """The list and the report render human-readable Catalan text."""
    empty = render_reviewer_list([])
    assert "No hi ha cap revisor" in empty
    listing = render_reviewer_list(
        [Reviewer(GLOBAL_SCOPE, "9", "Glob", NOW), Reviewer("-100", "222", "Pepe", NOW)]
    )
    assert "Global → Glob" in listing
    assert "-100 → Pepe" in listing
    report = render_report(
        [
            ReviewerEvent(
                feedback_id="fb:1",
                action="approved",
                created_at=NOW,
                reviewer_name="Pepe",
                group_label="Prebenjamins",
                question="Com es demana l'equipament?",
                approval_scope=GLOBAL_SCOPE,
            )
        ]
    )
    assert "Correccions revisades (1)" in report
    assert "Pepe · Prebenjamins" in report
    assert "aprovada (global)" in report


def _report_service(
    events: InMemoryReviewerEventRepository,
    transport,  # noqa: ANN001 - recording transport from fakes
    mode: str,
) -> ReviewerReportService:
    return ReviewerReportService(
        events=events,
        state=InMemoryReportStateRepository(),
        transport=transport,
        clock=FrozenClock(NOW),
        admin_user_id="1",
        mode=mode,
    )


def _event() -> ReviewerEvent:
    return ReviewerEvent(
        feedback_id="fb:1",
        action="approved",
        created_at=NOW,
        reviewer_name="Pepe",
        group_label="Prebenjamins",
        question="Com?",
        approval_scope=GLOBAL_SCOPE,
    )


def test_always_mode_reports_immediately() -> None:
    """With mode=always every recorded event reaches the admin at once."""
    events = InMemoryReviewerEventRepository()
    transport = RecordingTransportStub()
    service = _report_service(events, transport, "always")
    asyncio.run(service.record(_event()))
    assert len(transport.messages) == 1
    assert asyncio.run(events.list_unreported()) == []


def test_batch_mode_holds_until_due() -> None:
    """With mode=batch events accumulate until the interval elapses."""
    events = InMemoryReviewerEventRepository()
    transport = RecordingTransportStub()
    service = _report_service(events, transport, "batch")
    asyncio.run(service.record(_event()))
    assert transport.messages == []
    assert asyncio.run(service.maybe_send()) is True
    assert len(transport.messages) == 1
    assert asyncio.run(service.maybe_send()) is False


def test_off_mode_never_reports() -> None:
    """With mode=off nothing is ever sent to the admin."""
    events = InMemoryReviewerEventRepository()
    transport = RecordingTransportStub()
    service = _report_service(events, transport, "off")
    asyncio.run(service.record(_event()))
    assert asyncio.run(service.maybe_send()) is False
    assert transport.messages == []


def test_settings_reject_unknown_report_mode() -> None:
    """ADMIN_REPORT_MODE only accepts the three documented values."""
    with pytest.raises(ValueError, match="ADMIN_REPORT_MODE"):
        Settings(admin_report_mode="hourly")


class RecordingTransportStub:
    """Minimal recording transport for the report service."""

    def __init__(self) -> None:
        """Create an empty recording transport."""
        self.messages: list[tuple[str, str]] = []

    async def send_message(self, conversation_id: str, text: str) -> str | None:
        """Record a message."""
        self.messages.append((conversation_id, text))
        return str(len(self.messages))
