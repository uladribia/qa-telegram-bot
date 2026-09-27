# SPDX-License-Identifier: MIT
"""Channel-independent question and answer DTOs.

These models describe the canonical ``/v1`` contract. They know nothing about
how a question arrived, so the Telegram adapter and any future channel answer
through exactly the same application services.
"""

from pydantic import BaseModel, Field

from knowledge_bot.domain.enums import AnswerMode


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
