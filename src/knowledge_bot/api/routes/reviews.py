# SPDX-License-Identifier: MIT
"""Canonical review routes for reviewers."""

from fastapi import APIRouter, HTTPException, Request

from knowledge_bot.api.context import ContextResolver, InternalKey, internal_context
from knowledge_bot.domain.enums import ReviewAction
from knowledge_bot.domain.errors import InvalidTransitionError
from knowledge_bot.models.feedback import (
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewDraftRequest,
)


def build_reviews_router(resolve_context: ContextResolver) -> APIRouter:
    """Build the reviewer-facing correction routes."""
    router = APIRouter()

    @router.put("/reviews/{feedback_id}/draft")
    async def update_review_draft(
        request: Request,
        feedback_id: str,
        body: ReviewDraftRequest,
        key: InternalKey = None,
    ) -> dict[str, str]:
        """Update a correction draft after scope-aware authorization."""
        context = await internal_context(request, key, resolve_context)
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
        key: InternalKey = None,
    ) -> ReviewDecisionResponse:
        """Approve or reject a correction after scope-aware authorization."""
        context = await internal_context(request, key, resolve_context)
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
        try:
            version = await context.feedback.approve(feedback_id, body.action)
        except InvalidTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if version is None:
            raise HTTPException(status_code=409, detail="already resolved")
        attempt = await context.reindex.try_reindex_qa_version(version.id)
        return ReviewDecisionResponse(
            status="approved",
            feedback_id=feedback_id,
            qa_item_id=version.qa_id,
            qa_version_id=version.id,
            projection_status=attempt.status,
        )

    return router
