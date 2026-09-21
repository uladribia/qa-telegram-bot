# SPDX-License-Identifier: MIT
"""Build the human knowledge review report (read-only).

The report groups the current answers of every scope by canonical question and
flags the cases a human should look at:

- a group variant whose answer differs from the global answer;
- a question whose current answer is an approved correction;
- entries still under review;
- a web renewal that overwrote an approved correction.

Acting on the report goes through the normal Telegram correction flow; the
report itself changes nothing.
"""

from dataclasses import dataclass, field

from knowledge_bot.domain.scope import Scope, is_global
from knowledge_bot.ports.repositories import ConversationRepository
from knowledge_bot.ports.review import ReviewItem, ReviewSource

CORRECTION_ORIGIN = "admin_approved"
WEB_ORIGIN = "web_seed"


@dataclass(frozen=True, slots=True)
class ReviewEntry:
    """One canonical question and its current answers across scopes."""

    question: str
    global_answer: str | None
    variants: list[tuple[Scope, str]]
    divergent: bool = False
    under_review: bool = False
    corrected: bool = False
    renewal_overrode_correction: bool = False


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """The review report as structured entries."""

    entries: list[ReviewEntry] = field(default_factory=list)


def _build_entry(items: list[ReviewItem]) -> ReviewEntry:
    """Collapse one canonical question's items into a review entry."""
    question = items[0].question
    global_answer: str | None = None
    variants: list[tuple[Scope, str]] = []
    under_review = False
    corrected = False
    renewal_overrode = False
    for item in items:
        if item.status == "under_review":
            under_review = True
            continue
        if item.origin == CORRECTION_ORIGIN:
            corrected = True
        if item.origin == WEB_ORIGIN and item.superseded_origin == CORRECTION_ORIGIN:
            renewal_overrode = True
        if is_global(item.scope):
            global_answer = item.answer
        else:
            variants.append((item.scope, item.answer))
    return ReviewEntry(
        question=question,
        global_answer=global_answer,
        variants=variants,
        divergent=global_answer is not None
        and any(answer != global_answer for _, answer in variants),
        under_review=under_review,
        corrected=corrected,
        renewal_overrode_correction=renewal_overrode,
    )


def render_review_report(entries: list[ReviewEntry]) -> str:
    """Render the review report as markdown.

    Args:
        entries: The collapsed review entries.

    Returns:
        The markdown report text.
    """
    lines = ["# Knowledge review report", ""]
    if not entries:
        lines.append("Cap trobada per revisar: tot quadra.")
        return "\n".join(lines)
    for entry in entries:
        lines.append(f"## {entry.question}")
        lines.append("")
        if entry.global_answer is not None:
            lines.append(f"- **global**: {entry.global_answer}")
        for scope, answer in entry.variants:
            lines.append(f"- **{scope}**: {answer}")
        flags: list[str] = []
        if entry.under_review:
            flags.append("sota revisió")
        if entry.corrected:
            flags.append("correcció aprovada")
        if entry.renewal_overrode_correction:
            flags.append("renovació web sobre una correcció")
        if entry.divergent:
            flags.append("variant de grup diferent de la global")
        if flags:
            lines.append("")
            lines.append("⚠️ " + "; ".join(flags))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


@dataclass(frozen=True, slots=True)
class ReviewService:
    """Build the human knowledge review report."""

    source: ReviewSource
    conversations: ConversationRepository | None = None

    async def review(self) -> list[ReviewEntry]:
        """Return the review entries worth human attention.

        Returns:
            One entry per canonical question that has a finding.
        """
        items = await self.source.list_current()
        by_key: dict[str, list[ReviewItem]] = {}
        for item in items:
            by_key.setdefault(item.canonical_key, []).append(item)
        entries = [_build_entry(group) for group in by_key.values()]
        for entry in entries:
            # Variant slots carry the display label from here on (frozen dataclass:
            # the list itself stays mutable).
            entry.variants[:] = [
                (await self._scope_label(scope), answer)
                for scope, answer in entry.variants
            ]
        return [entry for entry in entries if _has_finding(entry)]

    async def _scope_label(self, scope: Scope) -> str:
        """Label a scope for the report: group title, or the id as fallback."""
        if is_global(scope):
            return "global"
        if self.conversations is not None:
            conversation = await self.conversations.get(scope)
            if conversation is not None and conversation.title:
                return f"grup {conversation.title}"
        return f"grup {scope}"


def _has_finding(entry: ReviewEntry) -> bool:
    """Return whether an entry has anything a human should review.

    Args:
        entry: The collapsed entry.

    Returns:
        ``True`` when the entry carries a finding.
    """
    return (
        entry.under_review
        or entry.corrected
        or entry.renewal_overrode_correction
        or entry.divergent
    )
