# SPDX-License-Identifier: MIT
"""Canonical correction routes for reporters."""

from fastapi import APIRouter, HTTPException, Request

from knowledge_bot.api.context import ContextResolver, InternalKey, internal_context
from knowledge_bot.models.feedback import (
    FeedbackProposalRequest,
    StartFeedbackRequest,
    StartFeedbackResponse,
)


def build_feedback_router(resolve_context: ContextResolver) -> APIRouter:
    """Build the reporter-facing correction routes."""
    router = APIRouter()

    @router.post("/feedback", response_model=StartFeedbackResponse)
    async def start_feedback(
        request: Request,
        body: StartFeedbackRequest,
        key: InternalKey = None,
    ) -> StartFeedbackResponse:
        """Start a correction for a stored answer."""
        context = await internal_context(request, key, resolve_context)
        feedback = await context.feedback.start(
            body.answer_id,
            body.reporter_principal_id,
            reporter_name=body.reporter_name,
        )
        answer = await context.answer.get_answer(body.answer_id)
        if feedback is None or answer is None:
            raise HTTPException(status_code=404, detail="answer not found")
        return StartFeedbackResponse(
            feedback_id=feedback.id,
            question=answer.question,
            current_answer=answer.answer,
            status=feedback.status.value,
        )

    @router.put("/feedback/{feedback_id}/proposal")
    async def submit_proposal(
        request: Request,
        feedback_id: str,
        body: FeedbackProposalRequest,
        key: InternalKey = None,
    ) -> dict[str, str]:
        """Submit the reporter's proposed correction."""
        context = await internal_context(request, key, resolve_context)
        feedback = await context.feedback.get_feedback(feedback_id)
        if feedback is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        if feedback.reporter_principal_id != body.reporter_principal_id:
            raise HTTPException(status_code=403, detail="not the reporter")
        updated = await context.feedback.propose(
            feedback_id, body.proposal, body.reporter_principal_id
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        return {"feedback_id": updated.id, "status": updated.status.value}

    return router
