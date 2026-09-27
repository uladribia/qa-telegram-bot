# SPDX-License-Identifier: MIT
"""Request models for the internal operator endpoints.

These DTOs drive maintenance, evaluation, and seeding work. They are not part
of the channel-independent product contract: no user-facing flow reads them.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from knowledge_bot.models.messages import NormalizedMessage
from knowledge_bot.models.seed import SeedQA


class FrozenEvidence(BaseModel):
    """One piece of evidence supplied by an evaluation case.

    A frozen case decides the generator's behaviour given exactly this
    evidence, so the case carries the text the model would have seen.
    """

    source_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    label: str = "Q&A"
    authority: int = Field(default=50, ge=0, le=100)
    kind: Literal["qa", "message"] = "qa"


class EvalAnswerRequest(BaseModel):
    """One explicit live-answer evaluation question."""

    question: str = Field(default="", min_length=1, max_length=4000)
    space_id: str | None = None
    evidence: list[FrozenEvidence] | None = None


class ReindexRequest(BaseModel):
    """Optional bounded incremental reindex request."""

    qa_after: str | None = None
    msg_after: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class IndexRepairRequest(BaseModel):
    """Bounded projection repair request."""

    limit: int = Field(default=100, ge=1, le=100)


class BackgroundBacklogRequest(BaseModel):
    """Bounded deferred-background processing request."""

    limit: int = Field(default=100, ge=1, le=100)


class RevertRequest(BaseModel):
    """Administrative Q&A revert request.

    ``qa_item_id`` defaults to empty so a body-less request reaches the route's
    own guard, which answers 422 instead of failing model construction.
    """

    qa_item_id: str = Field(default="")


class PromoteRequest(BaseModel):
    """Administrative Q&A promotion request.

    ``target`` is a Q&A item id or the canonical question text as the operator
    reads it in the review report. It defaults to empty so a body-less request
    reaches the route's own guard, which answers 422 instead of failing model
    construction.
    """

    target: str = Field(default="")


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
