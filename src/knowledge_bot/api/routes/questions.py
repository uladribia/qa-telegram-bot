# SPDX-License-Identifier: MIT
"""Canonical question route."""

from fastapi import APIRouter, HTTPException, Request

from knowledge_bot.api.context import ContextResolver, InternalKey, internal_context
from knowledge_bot.models.questions import AskQuestionRequest, AskQuestionResponse


def build_questions_router(resolve_context: ContextResolver) -> APIRouter:
    """Build the channel-independent question routes."""
    router = APIRouter()

    @router.post("/questions", response_model=AskQuestionResponse)
    async def ask_question(
        request: Request,
        body: AskQuestionRequest,
        key: InternalKey = None,
    ) -> AskQuestionResponse:
        """Answer an idempotent question without channel delivery."""
        context = await internal_context(request, key, resolve_context)
        try:
            return await context.answer.answer_request(body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return router
