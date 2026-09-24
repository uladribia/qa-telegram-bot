# SPDX-License-Identifier: MIT
"""Pydantic request contracts for HTTP boundaries."""

from pydantic import BaseModel, Field

from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA


class EvalAnswerRequest(BaseModel):
    """One explicit live-answer evaluation question."""

    question: str = Field(default="", min_length=1, max_length=4000)


class ReindexRequest(BaseModel):
    """Optional bounded incremental reindex request."""

    qa_after: str | None = None
    msg_after: str | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)


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
