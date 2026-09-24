# SPDX-License-Identifier: MIT
"""Tests for principal-based reviewer routing and authorization."""

import asyncio
from datetime import UTC, datetime

from knowledge_bot.application.reviewers import (
    ReviewerManager,
    ReviewerRouter,
    render_reviewer_list,
)
from knowledge_bot.domain.entities import Reviewer
from knowledge_bot.domain.enums import ReviewAction
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from tests.fakes.repositories import InMemoryReviewerRepository
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 1, 1, tzinfo=UTC)
GROUP = scope_for_space("sp_" + "1" * 32)


def test_nominate_upserts_and_remove_deletes() -> None:
    """Nominating replaces the scope reviewer and removing clears it."""
    manager = ReviewerManager(InMemoryReviewerRepository(), FrozenClock(NOW))
    assert (
        asyncio.run(manager.nominate(GROUP, "telegram:222", "Pepe", "telegram:1"))
        is False
    )
    assert (
        asyncio.run(manager.nominate(GROUP, "telegram:333", "Marta", "telegram:1"))
        is True
    )
    reviewer = asyncio.run(manager.reviewers.get(GROUP))
    assert reviewer is not None and reviewer.principal_id == "telegram:333"
    assert asyncio.run(manager.remove(GROUP)) is True


def test_router_falls_back_from_local_to_global_to_admin() -> None:
    """Reviewer routing prefers local, then global, then admin."""
    repository = InMemoryReviewerRepository()
    router = ReviewerRouter(repository, "telegram:1")
    asyncio.run(
        repository.save(Reviewer(GROUP, "telegram:222", "Pepe", NOW, "telegram:1"))
    )
    assert asyncio.run(router.destination("sp_" + "1" * 32)) == "telegram:222"
    asyncio.run(
        repository.save(
            Reviewer(GLOBAL_SCOPE, "telegram:9", "Global", NOW, "telegram:1")
        )
    )
    assert asyncio.run(router.destination("sp_" + "2" * 32)) == "telegram:9"
    asyncio.run(repository.delete(GLOBAL_SCOPE))
    assert asyncio.run(router.destination(None)) == "telegram:1"


def test_reviewer_permission_matrix() -> None:
    """Local, global, admin, and stranger permissions are explicit."""
    repository = InMemoryReviewerRepository()
    router = ReviewerRouter(repository, "telegram:admin")
    asyncio.run(
        repository.save(
            Reviewer(GROUP, "telegram:local", "Local", NOW, "telegram:admin")
        )
    )
    asyncio.run(
        repository.save(
            Reviewer(GLOBAL_SCOPE, "telegram:global", "Global", NOW, "telegram:admin")
        )
    )
    space = "sp_" + "1" * 32
    assert asyncio.run(
        router.can_confirm("telegram:local", space, ReviewAction.APPROVE_LOCAL)
    )
    assert not asyncio.run(
        router.can_confirm("telegram:local", space, ReviewAction.APPROVE_GLOBAL)
    )
    assert asyncio.run(
        router.can_confirm("telegram:global", space, ReviewAction.APPROVE_GLOBAL)
    )
    assert asyncio.run(
        router.can_confirm("telegram:admin", space, ReviewAction.APPROVE_GLOBAL)
    )
    assert not asyncio.run(
        router.can_confirm("telegram:stranger", space, ReviewAction.REJECT)
    )


def test_render_reviewer_list() -> None:
    """Reviewer listing is human-readable."""
    assert "No hi ha cap revisor" in render_reviewer_list([])
    listing = render_reviewer_list([Reviewer(GLOBAL_SCOPE, "telegram:9", "Glob", NOW)])
    assert "Global" in listing and "Glob" in listing
