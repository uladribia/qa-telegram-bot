# SPDX-License-Identifier: MIT
"""Generic channel-independent application REST routes."""

from collections.abc import Awaitable, Callable
from typing import Annotated, cast

from fastapi import APIRouter, Header, HTTPException, Request

from knowledge_bot.application.feedback import GROUP_SCOPE
from knowledge_bot.contracts.api import (
    AskQuestionRequest,
    AskQuestionResponse,
    FeedbackProposalRequest,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewDraftRequest,
    StartFeedbackRequest,
    StartFeedbackResponse,
)
from knowledge_bot.domain.enums import ReviewAction
from knowledge_bot.domain.scope import GLOBAL_SCOPE
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.infrastructure.security import secrets_match


def build_api_router(
    resolve_context: Callable[[Request], AppContext | Awaitable[AppContext]],
) -> APIRouter:
    """Build authenticated generic application routes."""
    router = APIRouter(prefix="/v1")

    async def context_for(
        request: Request, key: Annotated[str | None, Header(alias="X-Internal-Key")]
    ) -> AppContext:
        context = resolve_context(request)
        if isinstance(context, Awaitable):
            context = await cast(Awaitable[AppContext], context)
        else:
            context = cast(AppContext, context)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        return context

    @router.post("/questions", response_model=AskQuestionResponse)
    async def ask_question(
        request: Request,
        body: AskQuestionRequest,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> AskQuestionResponse:
        """Answer an idempotent question without channel delivery."""
        context = await context_for(request, key)
        try:
            return await context.answer.answer_request(body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/feedback", response_model=StartFeedbackResponse)
    async def start_feedback(
        request: Request,
        body: StartFeedbackRequest,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> StartFeedbackResponse:
        """Start a correction for a stored answer."""
        context = await context_for(request, key)
        feedback = await context.feedback.start(
            body.answer_id,
            body.reporter_principal_id,
            reporter_name=body.reporter_name,
        )
        answer = await context.answer.answers.get(body.answer_id)
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
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Submit the reporter's proposed correction."""
        context = await context_for(request, key)
        feedback = await context.feedback.feedback.get(feedback_id)
        if feedback is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        if feedback.reporter_hash != body.reporter_principal_id:
            raise HTTPException(status_code=403, detail="not the reporter")
        updated = await context.feedback.propose(feedback_id, body.proposal)
        if updated is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        return {"feedback_id": updated.id, "status": updated.status.value}

    @router.put("/reviews/{feedback_id}/draft")
    async def update_review_draft(
        request: Request,
        feedback_id: str,
        body: ReviewDraftRequest,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Update a correction draft after scope-aware authorization."""
        context = await context_for(request, key)
        review = await context.feedback.correction_request(feedback_id)
        if review is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        if not await context.router.can_confirm(
            body.actor_principal_id,
            review.origin_space_id,
            ReviewAction.EDIT,
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        updated = await context.feedback.admin_edit(feedback_id, body.text)
        if updated is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        return {"feedback_id": updated.id, "status": updated.status.value}

    @router.post(
        "/reviews/{feedback_id}/decision", response_model=ReviewDecisionResponse
    )
    async def decide_review(
        request: Request,
        feedback_id: str,
        body: ReviewDecisionRequest,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> ReviewDecisionResponse:
        """Approve or reject a correction after scope-aware authorization."""
        context = await context_for(request, key)
        review = await context.feedback.correction_request(feedback_id)
        if review is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        if not await context.router.can_confirm(
            body.actor_principal_id,
            review.origin_space_id,
            body.action,
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        if body.action.value == "reject":
            updated = await context.feedback.reject(feedback_id)
            if updated is None:
                raise HTTPException(status_code=409, detail="already resolved")
            return ReviewDecisionResponse(
                status=updated.status.value,
                feedback_id=updated.id,
                projection_status="not_applicable",
            )
        target = (
            GLOBAL_SCOPE if body.action == ReviewAction.APPROVE_GLOBAL else GROUP_SCOPE
        )
        version = await context.feedback.approve(feedback_id, target)
        if version is None:
            raise HTTPException(status_code=409, detail="already resolved")
        projected = await context.reindex.reindex_qa_version(version.id)
        return ReviewDecisionResponse(
            status="approved",
            feedback_id=feedback_id,
            qa_item_id=version.qa_id,
            qa_version_id=version.id,
            projection_status="indexed" if projected else "pending",
        )

    return router
