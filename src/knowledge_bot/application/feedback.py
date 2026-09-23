# SPDX-License-Identifier: MIT
"""Feedback and correction flow (spec §21-§26).

Everything happens inside Telegram: a user marks an answer wrong, proposes a
correction, the admin approves/edits/rejects it privately, and an approved
correction becomes a new, highest-authority Q&A version. History is never
mutated destructively.
"""

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime

from knowledge_bot.domain.entities import Feedback, QAEvidence, QAItem, QAVersion
from knowledge_bot.domain.enums import (
    EvidenceType,
    FeedbackStatus,
    QAOrigin,
    QAStatus,
)
from knowledge_bot.domain.policies import Authority
from knowledge_bot.domain.scope import Scope
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import (
    BotAnswerRepository,
    ConversationRepository,
    FeedbackRepository,
    QAEvidenceRepository,
    QAItemRepository,
    QAVersionRepository,
)

#: Approve target meaning "the conversation the corrected answer came from".
GROUP_SCOPE = "group"

START_PREFIX = "feedback:start:"
APPROVE_GLOBAL_PREFIX = "feedback:approve-global:"
APPROVE_GROUP_PREFIX = "feedback:approve-group:"
EDIT_PREFIX = "feedback:edit:"
REJECT_PREFIX = "feedback:reject:"

PROPOSAL_PROMPT = (
    "Què corregiries? Escriu la resposta correcta o explica què està malament."
)
PROPOSAL_ACK = "Gràcies. Ho he enviat a revisió."
EDIT_PROMPT = "Envia'm el text correcte."
REVIEW_REJECTED = "\u274c Correcci\u00f3 rebutjada."


_ACTIONS: tuple[tuple[str, str], ...] = (
    (START_PREFIX, "start"),
    (APPROVE_GLOBAL_PREFIX, "approve_global"),
    (APPROVE_GROUP_PREFIX, "approve_group"),
    (EDIT_PREFIX, "edit"),
    (REJECT_PREFIX, "reject"),
)


def canonical_key_for(question: str) -> str:
    """Return the canonical key for a question.

    Args:
        question: The question text.

    Returns:
        A short, stable key.
    """
    return hashlib.sha256(question.casefold().strip().encode("utf-8")).hexdigest()[:16]


def callback_action(data: str | None) -> str | None:
    """Return the action encoded in a callback payload.

    Args:
        data: The raw callback data.

    Returns:
        One of ``start``/``approve``/``edit``/``reject``, or ``None``.
    """
    for prefix, action in _ACTIONS:
        if data and data.startswith(prefix):
            return action
    return None


def callback_target(data: str | None) -> str | None:
    """Return the id encoded in a callback payload.

    Args:
        data: The raw callback data.

    Returns:
        The trailing id, or ``None``.
    """
    if not data:
        return None
    parts = data.split(":", 2)
    return parts[2] if len(parts) == 3 else None


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    """A correction proposal shown to its reviewer."""

    feedback_id: str
    question: str
    current_answer: str
    proposed_answer: str
    group_label: str | None = None
    current_origin: str | None = None
    group_chat_id: str | None = None


_CURRENT_ORIGIN_LABEL: dict[str, str] = {
    "web_seed": "web",
    "admin_approved": "correcció aprovada",
    "auto_generated": "generada del grup",
}


def current_origin_label(origin: str | None) -> str:
    """Return a human label for the origin of the current answer.

    Args:
        origin: The Q&A origin, or ``None`` when the answer was synthesized
            from group messages without a Q&A version.

    Returns:
        A short Catalan label for the review message.
    """
    if origin is None:
        return "síntesi del grup"
    return _CURRENT_ORIGIN_LABEL.get(origin, origin)


def render_review(request: CorrectionRequest) -> str:
    """Render the private admin review message for a correction.

    Args:
        request: The correction to review.

    Returns:
        The review text, including the current and proposed answers.
    """
    return (
        "\u26a0\ufe0f Correcci\u00f3 proposada\n\n"
        f"Grup: {request.group_label}\n\n"
        f"Pregunta:\n{request.question}\n\n"
        "Resposta actual ("
        f"{current_origin_label(request.current_origin)}):\n"
        f"{request.current_answer}\n\n"
        f"Proposta:\n{request.proposed_answer}"
    )


@dataclass(frozen=True, slots=True)
class FeedbackService:
    """Start, propose, and review corrections."""

    answers: BotAnswerRepository
    feedback: FeedbackRepository
    qa_items: QAItemRepository
    qa_versions: QAVersionRepository
    evidence: QAEvidenceRepository
    conversations: ConversationRepository
    clock: Clock

    async def start(
        self,
        answer_id: str,
        reporter_hash: str | None,
        reporter_chat_id: str | None = None,
        reporter_name: str | None = None,
    ) -> Feedback | None:
        """Open a correction proposal for a bot answer.

        Args:
            answer_id: The bot answer the user marked wrong.
            reporter_hash: A pseudonymized reporter id.
            reporter_chat_id: The chat to prompt privately.
            reporter_name: The reporter's display name, for the citation.

        Returns:
            The created feedback, or ``None`` when the answer is unknown.
        """
        answer = await self.answers.get(answer_id)
        if answer is None:
            return None
        # The id is deterministic, so re-flagging the same answer would collide
        # on insert. Reuse the live row for the new presser; a resolved row
        # gets a fresh, timestamped id so history stays intact.
        existing = await self.feedback.get(f"fb:{answer_id}")
        if existing is not None and existing.resolved_at is None:
            feedback = replace(
                existing,
                reporter_chat_id=reporter_chat_id,
                reporter_name=reporter_name,
            )
            await self.feedback.save(feedback)
            return feedback
        feedback = Feedback(
            id=(
                f"fb:{answer_id}:{int(self.clock.now().timestamp())}"
                if existing is not None
                else f"fb:{answer_id}"
            ),
            bot_answer_id=answer_id,
            status=FeedbackStatus.AWAITING_PROPOSAL,
            created_at=self.clock.now(),
            qa_id=answer.qa_version_id,
            reporter_hash=reporter_hash,
            reporter_chat_id=reporter_chat_id,
            reporter_name=reporter_name,
        )
        await self.feedback.add(feedback)
        return feedback

    async def _update(
        self,
        feedback: Feedback,
        *,
        status: FeedbackStatus,
        proposed_answer: str | None = None,
        admin_edited_answer: str | None = None,
        resolved: bool = False,
    ) -> Feedback:
        updated = replace(
            feedback,
            status=status,
            proposed_answer=proposed_answer,
            admin_edited_answer=admin_edited_answer,
            resolved_at=self.clock.now() if resolved else None,
        )
        await self.feedback.save(updated)
        return updated

    async def propose(
        self,
        feedback_id: str,
        proposed_answer: str,
        reporter_name: str | None = None,
    ) -> Feedback | None:
        """Record the correction proposed by the reporter.

        Args:
            feedback_id: The feedback being answered.
            proposed_answer: The text the reporter proposes.
            reporter_name: The proposer's display name, used as the citation
                author once the proposal is approved.

        Returns:
            The updated feedback, or ``None`` when it does not exist.
        """
        feedback = await self.feedback.get(feedback_id)
        if feedback is None:
            return None
        updated = replace(
            feedback,
            status=FeedbackStatus.PENDING_ADMIN,
            proposed_answer=proposed_answer,
            admin_edited_answer=feedback.admin_edited_answer,
            reporter_name=reporter_name or feedback.reporter_name,
            proposed_at=self.clock.now(),
        )
        await self.feedback.save(updated)
        return updated

    async def admin_edit(self, feedback_id: str, edited_answer: str) -> Feedback | None:
        """Record the admin's edited version, still pending approval.

        Args:
            feedback_id: The feedback being edited.
            edited_answer: The admin's corrected text.

        Returns:
            The updated feedback, or ``None`` when it does not exist.
        """
        feedback = await self.feedback.get(feedback_id)
        if feedback is None:
            return None
        return await self._update(
            feedback,
            status=FeedbackStatus.PENDING_ADMIN,
            proposed_answer=feedback.proposed_answer,
            admin_edited_answer=edited_answer,
        )

    async def reject(self, feedback_id: str) -> Feedback | None:
        """Reject a proposal without touching knowledge.

        Args:
            feedback_id: The feedback to reject.

        Returns:
            The updated feedback, or ``None`` when it does not exist.
        """
        feedback = await self.feedback.get(feedback_id)
        if feedback is None:
            return None
        return await self._update(
            feedback,
            status=FeedbackStatus.REJECTED,
            proposed_answer=feedback.proposed_answer,
            admin_edited_answer=feedback.admin_edited_answer,
            resolved=True,
        )

    async def correction_request(self, feedback_id: str) -> CorrectionRequest | None:
        """Build the admin review payload for a proposal.

        Args:
            feedback_id: The feedback to review.

        Returns:
            The review payload, or ``None`` when there is nothing to review.
        """
        feedback = await self.feedback.get(feedback_id)
        if feedback is None:
            return None
        answer = await self.answers.get(feedback.bot_answer_id)
        proposal = feedback.admin_edited_answer or feedback.proposed_answer
        if answer is None or not proposal:
            return None
        conversation = await self.conversations.get(answer.conversation_id)
        group_label = (conversation.title if conversation else None) or (
            answer.conversation_id
        )
        return CorrectionRequest(
            feedback_id=feedback.id,
            question=answer.question,
            current_answer=answer.answer,
            proposed_answer=proposal,
            group_label=group_label,
            current_origin=await self._current_origin(feedback.qa_id),
            group_chat_id=answer.conversation_id,
        )

    async def _current_origin(self, qa_ref: str | None) -> str | None:
        """Return the origin of the version the corrected answer came from.

        Args:
            qa_ref: The Q&A item or version id cited by the answer.

        Returns:
            The origin value, or ``None`` when unknown (synthesized answer).
        """
        cited = await self._cited_item(qa_ref) if qa_ref else None
        if cited is None or cited.current_version_id is None:
            return None
        version = await self.qa_versions.get(cited.current_version_id)
        return version.origin.value if version is not None else None

    async def approve(self, feedback_id: str, scope: Scope) -> QAVersion | None:
        """Approve a proposal as a new answer version in the chosen scope.

        Approving as global updates (or creates) the global item for the
        question. Approving as a group variant creates (or updates) an item
        scoped to the group the corrected answer came from, leaving the global
        answer untouched.

        Args:
            feedback_id: The feedback to approve.
            scope: ``GLOBAL_SCOPE``, or ``GROUP_SCOPE`` for the asking group.

        Returns:
            The new Q&A version, or ``None`` when the feedback is unknown or has
            no proposed answer.
        """
        feedback = await self.feedback.get(feedback_id)
        if feedback is None:
            return None
        answer_text = feedback.admin_edited_answer or feedback.proposed_answer
        if not answer_text:
            return None
        now = self.clock.now()
        answer = await self.answers.get(feedback.bot_answer_id)
        question = answer.question if answer is not None else ""
        target_scope = (
            answer.conversation_id if scope == GROUP_SCOPE and answer else scope
        )
        qa_id = await self._resolve_target(feedback, question, target_scope, now)
        item = await self.qa_items.get(qa_id)
        if item is None:
            return None
        version = QAVersion(
            id=f"qav:{feedback.id}:{int(now.timestamp())}",
            qa_id=qa_id,
            answer=answer_text,
            authority=int(Authority.ADMIN_APPROVED),
            origin=QAOrigin.ADMIN_APPROVED,
            created_at=feedback.proposed_at or now,
            created_by=feedback.reporter_name or "admin",
            author=feedback.reporter_name or "admin",
            supersedes_version_id=item.current_version_id,
        )
        await self.qa_versions.add(version)
        await self.qa_items.save(
            replace(
                item,
                status=QAStatus.ACTIVE,
                updated_at=now,
                current_version_id=version.id,
            )
        )
        await self.evidence.add(
            QAEvidence(
                qa_version_id=version.id,
                evidence_type=EvidenceType.MESSAGE,
                evidence_id=feedback.bot_answer_id,
            )
        )
        await self._update(
            feedback,
            status=FeedbackStatus.APPROVED,
            proposed_answer=feedback.proposed_answer,
            admin_edited_answer=feedback.admin_edited_answer,
            resolved=True,
        )
        return version

    async def _resolve_target(
        self,
        feedback: Feedback,
        question: str,
        scope: Scope,
        now: datetime,
    ) -> str:
        """Find or create the Q&A item an approval applies to.

        When the corrected answer cited an item in the target scope, that item
        is superseded. Otherwise the canonical question resolves (or creates)
        the item for the target scope.

        Args:
            feedback: The correction being approved.
            question: The question text of the corrected answer.
            scope: The scope the approval applies to.
            now: The current timestamp.

        Returns:
            The Q&A item id to attach the new version to.
        """
        if feedback.qa_id is not None:
            cited = await self._cited_item(feedback.qa_id)
            if cited is not None and cited.scope == scope:
                return cited.id
        key = canonical_key_for(question) if question else feedback.id
        existing = await self.qa_items.get_by_canonical_key(key, scope)
        if existing is not None:
            return existing.id
        created = QAItem(
            id=f"qa:{key}:{scope}",
            canonical_key=key,
            canonical_question=question,
            status=QAStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            scope=scope,
        )
        await self.qa_items.add(created)
        return created.id

    async def _cited_item(self, qa_ref: str) -> QAItem | None:
        """Resolve a feedback's Q&A reference to an item, if possible.

        Args:
            qa_ref: A Q&A item id or a Q&A version id.

        Returns:
            The referenced item, or ``None``.
        """
        item = await self.qa_items.get(qa_ref)
        if item is not None:
            return item
        source = await self.qa_versions.get(qa_ref)
        if source is None:
            return None
        return await self.qa_items.get(source.qa_id)
