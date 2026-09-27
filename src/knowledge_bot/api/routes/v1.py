# SPDX-License-Identifier: MIT
"""The canonical ``/v1`` product contract.

One router, assembled from the channel-independent route modules, so the
version prefix has exactly one home.
"""

from fastapi import APIRouter

from knowledge_bot.api.context import ContextResolver
from knowledge_bot.api.routes.feedback import build_feedback_router
from knowledge_bot.api.routes.questions import build_questions_router
from knowledge_bot.api.routes.reviews import build_reviews_router


def build_api_router(resolve_context: ContextResolver) -> APIRouter:
    """Build the authenticated generic application routes."""
    router = APIRouter(prefix="/v1")
    router.include_router(build_questions_router(resolve_context))
    router.include_router(build_feedback_router(resolve_context))
    router.include_router(build_reviews_router(resolve_context))
    return router
