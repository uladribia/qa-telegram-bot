# SPDX-License-Identifier: MIT
"""Pydantic request contracts for HTTP boundaries."""

from pydantic import BaseModel, Field, model_validator

from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.enums import AnswerMode, ReviewAction


class EvalAnswerRequest(BaseModel):
    """One explicit live-answer evaluation question."""

    question: str = Field(default="", min_length=1, max_length=4000)


class ReindexRequest(BaseModel):
    """Optional bounded incremental reindex request."""

    rebuild: bool = False
    qa_after: str | None = None
    msg_after: str | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)


class IndexRepairRequest(BaseModel):
    """Bounded projection repair request."""

    limit: int = Field(default=100, ge=1, le=100)


class BackgroundBacklogRequest(BaseModel):
    """Bounded deferred-background processing request."""

    limit: int = Field(default=100, ge=1, le=1000)


class RegisterGroupRequest(BaseModel):
    """Telegram connector registration request."""

    chat_id: str = Field(default="", min_length=1)
    title: str | None = None
    space_id: str | None = None


class RevertRequest(BaseModel):
    """Administrative Q&A revert request."""

    qa_item_id: str = Field(default="", min_length=1)


class SeedRequest(BaseModel):
    """Versioned Q&A and imported-message seed request."""

    qa: list[SeedQA] = Field(default_factory=list)
    messages: list[NormalizedMessage] = Field(default_factory=list)
    scope: str = "global"
    renew: bool = False


class DailyReportRequest(BaseModel):
    """Manual daily report execution options."""

    force: bool = False
    dry_run: bool = False

    @model_validator(mode="after")
    def _validate_mode(self) -> "DailyReportRequest":
        """Reject force plus dry-run."""
        if self.force and self.dry_run:
            raise ValueError("force and dry_run cannot both be true")  # noqa: TRY003
        return self


class AskQuestionRequest(BaseModel):
    """Channel-independent question request."""

    request_id: str = Field(min_length=1, max_length=200)
    space_id: str | None = None
    principal_id: str | None = None
    question: str = Field(min_length=1, max_length=4000)


class AnswerSource(BaseModel):
    """One structured source returned with an answer."""

    source_id: str
    kind: str
    label: str
    url: str | None = None
    author: str | None = None
    date: str | None = None


class AskQuestionResponse(BaseModel):
    """Channel-independent answer result."""

    answer_id: str
    mode: AnswerMode
    answer: str
    rendered_text: str
    sources: list[AnswerSource] = Field(default_factory=list)
    can_report: bool = True


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
