# SPDX-License-Identifier: MIT
"""Channel-independent correction and review DTOs.

A correction is the same operation whichever channel proposed it: a reporter
starts one, sends a proposal, and a reviewer approves or rejects it. Only the
prompt wording and the inline buttons are Telegram-specific.
"""

from pydantic import BaseModel, Field

from knowledge_bot.domain.enums import ReviewAction


class StartFeedbackRequest(BaseModel):
    """Start a correction for a stored answer."""

    answer_id: str = Field(min_length=1)
    reporter_principal_id: str = Field(min_length=1)
    reporter_name: str | None = None


class StartFeedbackResponse(BaseModel):
    """Correction state visible to a reporter."""

    feedback_id: str
    question: str
    current_answer: str
    status: str


class FeedbackProposalRequest(BaseModel):
    """Submit or replace a reporter's proposed correction."""

    reporter_principal_id: str = Field(min_length=1)
    proposal: str = Field(min_length=1, max_length=10000)


class ReviewDraftRequest(BaseModel):
    """Replace the review draft with reviewer-authored text."""

    actor_principal_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=10000)


class ReviewDecisionRequest(BaseModel):
    """Approve or reject one pending correction."""

    actor_principal_id: str = Field(min_length=1)
    action: ReviewAction


class ReviewDecisionResponse(BaseModel):
    """Result of one review decision."""

    status: str
    feedback_id: str
    qa_item_id: str | None = None
    qa_version_id: str | None = None
    projection_status: str
