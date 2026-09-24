# SPDX-License-Identifier: MIT
"""Reviewer nomination, routing, and authorization."""

from dataclasses import dataclass

from knowledge_bot.domain.entities import Reviewer
from knowledge_bot.domain.enums import ReviewAction
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import ReviewerRepository


def parse_reviewer_command(text: str) -> tuple[str, bool]:
    """Parse a reviewer command into action and global-scope intent."""
    tokens = text.split()[1:]
    return ("remove" if "off" in tokens else "nominate"), "global" in tokens


def render_reviewer_list(reviewers: list[Reviewer]) -> str:
    """Render reviewer assignments."""
    if not reviewers:
        return "No hi ha cap revisor nominat. Les correccions arriben a l'admin."
    return "Revisors:\n" + "\n".join(
        f"• {'Global' if item.scope == GLOBAL_SCOPE else item.scope} → {item.name}"
        for item in reviewers
    )


@dataclass(frozen=True, slots=True)
class ReviewerRouter:
    """Resolve reviewer destinations and authorize review actions."""

    reviewers: ReviewerRepository
    admin_principal_id: str

    async def destination(self, origin_space_id: str | None) -> str | None:
        """Return the local, global, or admin principal for a review."""
        if origin_space_id is not None:
            reviewer = await self.reviewers.get(scope_for_space(origin_space_id))
            if reviewer is not None:
                return reviewer.principal_id
        reviewer = await self.reviewers.get(GLOBAL_SCOPE)
        return (
            reviewer.principal_id if reviewer is not None else self.admin_principal_id
        )

    async def reviewer_name(
        self, principal_id: str | None, origin_space_id: str | None
    ) -> str:
        """Return a safe display name for a review principal."""
        if principal_id == self.admin_principal_id:
            return "admin"
        if origin_space_id is not None:
            reviewer = await self.reviewers.get(scope_for_space(origin_space_id))
            if reviewer is not None and reviewer.principal_id == principal_id:
                return reviewer.name
        reviewer = await self.reviewers.get(GLOBAL_SCOPE)
        return (
            reviewer.name
            if reviewer is not None and reviewer.principal_id == principal_id
            else "revisor"
        )

    async def can_approve_global(self, principal_id: str | None) -> bool:
        """Return whether a principal may approve global knowledge."""
        if principal_id is None:
            return False
        if principal_id == self.admin_principal_id:
            return True
        reviewer = await self.reviewers.get(GLOBAL_SCOPE)
        return reviewer is not None and reviewer.principal_id == principal_id

    async def can_confirm(
        self,
        principal_id: str | None,
        origin_space_id: str | None,
        action: ReviewAction,
    ) -> bool:
        """Return whether a principal may perform a requested action."""
        if principal_id is None:
            return False
        if principal_id == self.admin_principal_id:
            return True
        global_reviewer = await self.reviewers.get(GLOBAL_SCOPE)
        if global_reviewer is not None and global_reviewer.principal_id == principal_id:
            return True
        if origin_space_id is None or action is ReviewAction.APPROVE_GLOBAL:
            return False
        reviewer = await self.reviewers.get(scope_for_space(origin_space_id))
        return reviewer is not None and reviewer.principal_id == principal_id


@dataclass(frozen=True, slots=True)
class ReviewerManager:
    """Admin-only reviewer assignment operations."""

    reviewers: ReviewerRepository
    clock: Clock

    async def nominate(
        self, scope: str, principal_id: str, name: str, nominated_by_principal_id: str
    ) -> bool:
        """Create or replace a reviewer."""
        replaced = await self.reviewers.get(scope) is not None
        await self.reviewers.save(
            Reviewer(
                scope, principal_id, name, self.clock.now(), nominated_by_principal_id
            )
        )
        return replaced

    async def remove(self, scope: str) -> bool:
        """Remove a reviewer."""
        return await self.reviewers.delete(scope)

    async def list_reviewers(self) -> list[Reviewer]:
        """List all reviewers."""
        return await self.reviewers.all()
