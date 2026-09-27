# SPDX-License-Identifier: MIT
"""Internal operator endpoints: evaluation, maintenance, and seeding.

These are not part of the channel-independent product contract. They exist for
the CLI, the eval harness, and maintenance, and they are all gated on the
internal key. Routes whose body is channel-specific delegate to that channel's
adapter rather than naming it here.
"""

import time

from fastapi import APIRouter, Body, HTTPException, Request

from knowledge_bot.adapters.telegram.groups import bind_telegram_group
from knowledge_bot.adapters.telegram.models import RegisterGroupRequest
from knowledge_bot.api.context import ContextResolver, InternalKey, internal_context
from knowledge_bot.application.review import render_review_report
from knowledge_bot.domain.enums import AiWorkClass
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.models.operations import (
    BackgroundBacklogRequest,
    DailyReportRequest,
    EvalAnswerRequest,
    IndexRepairRequest,
    PromoteRequest,
    ReindexRequest,
    RevertRequest,
    SeedRequest,
)

# Live-eval protection: one request may carry at most this many queries, and an
# isolate admits this many evaluation calls per minute. An unbounded burst of
# evaluation traffic can wedge the Worker; the budget guard does not protect
# against that.
_MAX_EVAL_QUERIES = 20
_EVAL_CALLS_PER_MINUTE = 60
_EMPTY_EVAL_BODY = Body(default_factory=EvalAnswerRequest)
_EMPTY_REVERT_BODY = Body(default_factory=RevertRequest)
_EMPTY_PROMOTE_BODY = Body(default_factory=PromoteRequest)
_EMPTY_REINDEX_BODY = Body(default_factory=ReindexRequest)
_EMPTY_SEED_BODY = Body(default_factory=SeedRequest)
_EMPTY_BACKLOG_BODY = Body(default_factory=BackgroundBacklogRequest)
_EMPTY_REPAIR_BODY = Body(default_factory=IndexRepairRequest)
_EMPTY_GROUP_BODY = Body(default_factory=RegisterGroupRequest)
_EMPTY_DAILY_REPORT_BODY = Body(default_factory=DailyReportRequest)


async def _require_evaluation_budget(context: AppContext) -> None:
    """Refuse an expensive admin run once the day's AI budget is nearly spent.

    Only evals and reindex consult this. A real user question is never refused
    here: it degrades to the temporary-unavailable reply instead, so the inbound
    event is never lost (spec §47).

    Args:
        context: The application context.

    Raises:
        HTTPException: 429 when the evaluation ceiling has been reached.
    """
    if await context.budget.evaluation_allowed():
        return
    spend = await context.budget.spend()
    raise HTTPException(
        status_code=429,
        detail=(
            f"AI budget for evaluations is spent: {spend.neurons:.0f} of "
            f"{spend.limit:.0f} estimated neurons used today. Real answers keep "
            "working. Resets at 00:00 UTC."
        ),
    )


def build_internal_router(resolve_context: ContextResolver) -> APIRouter:
    """Build the authenticated internal operator routes."""
    router = APIRouter()
    eval_calls: list[float] = []

    def _admit_eval_call() -> None:
        """Throttle live-eval calls per isolate.

        Raises:
            HTTPException: When the isolate already served its per-minute
                allowance of evaluation calls.
        """
        now = time.monotonic()
        cutoff = now - 60.0
        eval_calls[:] = [stamp for stamp in eval_calls if stamp > cutoff]
        if len(eval_calls) >= _EVAL_CALLS_PER_MINUTE:
            raise HTTPException(
                status_code=429,
                detail=(
                    "too many evaluation calls in this isolate; "
                    "pace the live eval and retry shortly"
                ),
            )
        eval_calls.append(now)

    @router.post("/internal/eval/answer")
    async def internal_eval_answer(
        request: Request,
        body: EvalAnswerRequest = _EMPTY_EVAL_BODY,
        key: InternalKey = None,
    ) -> dict[str, object]:
        """Answer a question without sending it, for live answer evals.

        Returns the decided mode, the semantic reason, the rendered answer, the
        citations, and the retrieved candidates with their similarities, so a
        failed evaluation case can be attributed without reading production
        logs. Supplying ``evidence`` runs the generator against exactly that
        evidence and bypasses retrieval, which is how the frozen-generation
        suite isolates the generator. No message ever reaches Telegram.
        """
        context = await internal_context(request, key, resolve_context)
        if not body.question:
            raise HTTPException(status_code=422, detail="question required")
        _admit_eval_call()
        await _require_evaluation_budget(context)
        question = body.question
        preview = await context.answer.dry_run(
            question,
            evidence=(
                context.answer.frozen_evidence(
                    [item.model_dump() for item in body.evidence]
                )
                if body.evidence is not None
                else None
            ),
        )
        outcome = preview.outcome
        return {
            "question": question,
            "mode": outcome.mode.value,
            "reason": outcome.reason.value,
            "answer": outcome.answer,
            "text": outcome.text,
            "source_ids": outcome.source_ids,
            "evidence_ids": [item.source_id for item in preview.evidence],
            "candidates": [
                {
                    "source_id": item.source_id,
                    "similarity": round(item.similarity, 4),
                    "kind": "qa" if item.qa_version_id is not None else "message",
                    "authority": item.authority,
                }
                for item in preview.evidence
            ],
            "citations": [
                {
                    "source_id": item.source_id,
                    "label": item.label,
                    "url": item.url,
                    "author": item.author,
                    "date": item.date,
                    "text": item.text,
                }
                for item in preview.evidence
                if item.source_id in outcome.source_ids
            ],
        }

    @router.post("/internal/jobs/daily-report")
    async def internal_daily_report(
        request: Request,
        body: DailyReportRequest = _EMPTY_DAILY_REPORT_BODY,
        key: InternalKey = None,
    ) -> dict[str, str]:
        """Run the same deterministic report job as the scheduled handler."""
        context = await internal_context(request, key, resolve_context)
        if body.dry_run:
            return {"status": "preview", "report": await context.daily_report.preview()}
        sent = await context.daily_report.run(force=body.force)
        return {"status": "sent" if sent else "skipped"}

    @router.post("/internal/revert")
    async def internal_revert(
        request: Request,
        body: RevertRequest = _EMPTY_REVERT_BODY,
        key: InternalKey = None,
    ) -> dict[str, object]:
        """Revert a Q&A item to the version its current one superseded.

        The only rollback path: a CLI operation, never a Telegram action.
        """
        context = await internal_context(request, key, resolve_context)
        if not body.qa_item_id:
            raise HTTPException(status_code=422, detail="qa_item_id required")
        restored = await context.reverter.revert(body.qa_item_id)
        if restored is None:
            raise HTTPException(status_code=404, detail="nothing to revert")
        attempt = await context.reindex.try_reindex_qa_version(restored.id)
        return {
            "status": "reverted",
            "restored_version_id": restored.id,
            "projection_status": attempt.status,
        }

    @router.post("/internal/promote")
    async def internal_promote(
        request: Request,
        body: PromoteRequest = _EMPTY_PROMOTE_BODY,
        key: InternalKey = None,
    ) -> dict[str, object]:
        """Publish a seeded ``under_review`` Q&A item and project it.

        A CLI operation, never a Telegram action. The reviewer flow does not
        need it: approving a correction already writes the item back as active.
        """
        context = await internal_context(request, key, resolve_context)
        if not body.target.strip():
            raise HTTPException(status_code=422, detail="target required")
        item = await context.promoter.promote(body.target)
        if item is None:
            raise HTTPException(status_code=404, detail="no such q&a item")
        version_id = item.current_version_id
        projection = (
            "no_current_version"
            if version_id is None
            else (await context.reindex.try_reindex_qa_version(version_id)).status
        )
        return {
            "status": item.status.value,
            "qa_item_id": item.id,
            "canonical_question": item.canonical_question,
            "projection_status": projection,
        }

    @router.post("/internal/index/cleanup")
    async def internal_index_cleanup(
        request: Request,
        key: InternalKey = None,
    ) -> dict[str, int]:
        """Delete known derived vectors without embedding anything."""
        context = await internal_context(request, key, resolve_context)
        report = await context.reindex.cleanup_projection()
        return {"removed": report.removed}

    @router.post("/internal/reindex")
    async def internal_reindex(
        request: Request,
        body: ReindexRequest = _EMPTY_REINDEX_BODY,
        key: InternalKey = None,
    ) -> dict[str, object]:
        """Project one bounded batch from SQL truth."""
        context = await internal_context(request, key, resolve_context)
        await _require_evaluation_budget(context)
        report = await context.reindex.reindex(
            qa_after=body.qa_after,
            msg_after=body.msg_after,
            limit=body.limit,
        )
        return {
            "qa": report.qa,
            "messages": report.messages,
            "qa_after": report.next_qa,
            "msg_after": report.next_msg,
        }

    @router.post("/internal/retrieve")
    async def internal_retrieve(
        request: Request,
        key: InternalKey = None,
    ) -> dict[str, dict[str, list[str]]]:
        """Return the retrieved source ids for each query (eval support)."""
        context = await internal_context(request, key, resolve_context)
        _admit_eval_call()
        await _require_evaluation_budget(context)
        payload = await request.json()
        queries = payload.get("queries", []) if isinstance(payload, dict) else []
        if not isinstance(queries, list):
            queries = []
        if len(queries) > _MAX_EVAL_QUERIES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"at most {_MAX_EVAL_QUERIES} queries per request; "
                    "split the live eval into chunks"
                ),
            )
        results: dict[str, list[str]] = {}
        for query in queries:
            retrieved = await context.answer.retrieval.retrieve(str(query))
            results[str(query)] = [
                item.anchor or item.source_id for item in retrieved.qa
            ]
        return {"results": results}

    @router.post("/internal/seed")
    async def internal_seed(
        request: Request,
        body: SeedRequest = _EMPTY_SEED_BODY,
        key: InternalKey = None,
    ) -> dict[str, int]:
        """Seed Q&A entries and imported messages into D1."""
        context = await internal_context(request, key, resolve_context)
        created, skipped, renewed, diverged, version_ids = await context.seed.seed_qa(
            body.qa, body.scope, body.renew
        )
        indexed = 0
        for version_id in version_ids:
            attempt = await context.reindex.try_reindex_qa_version(version_id)
            if attempt.status == "indexed":
                indexed += 1
        message_count = await context.seed.seed_messages(body.messages, body.scope)
        return {
            "qa": created,
            "qa_skipped": skipped,
            "indexed": indexed,
            "qa_renewed": renewed,
            "qa_diverged": diverged,
            "messages": message_count,
        }

    @router.post("/internal/smoke/runtime")
    async def internal_runtime_smoke(
        request: Request,
        key: InternalKey = None,
    ) -> dict[str, object]:
        """Run the explicitly authorized tiny runtime smoke."""
        context = await internal_context(request, key, resolve_context)
        return await context.runtime_smoke.run()

    @router.post("/internal/index/repair")
    async def internal_index_repair(
        request: Request,
        body: IndexRepairRequest = _EMPTY_REPAIR_BODY,
        key: InternalKey = None,
    ) -> dict[str, int | bool]:
        """Repair a bounded batch of pending or failed projections."""
        context = await internal_context(request, key, resolve_context)
        report = await context.projector.repair(body.limit)
        return {
            "repaired": report.repaired,
            "removed": report.removed,
            "failed": report.failed,
            "stopped_by_budget": report.stopped_by_budget,
        }

    @router.post("/internal/background/process-backlog")
    async def internal_process_background_backlog(
        request: Request,
        body: BackgroundBacklogRequest = _EMPTY_BACKLOG_BODY,
        key: InternalKey = None,
    ) -> dict[str, int | bool]:
        """Process a bounded batch of budget-deferred background messages."""
        context = await internal_context(request, key, resolve_context)
        if not await context.budget.work_allowed(AiWorkClass.MAINTENANCE):
            raise HTTPException(status_code=429, detail="maintenance budget exhausted")
        result = await context.background_indexer.process_backlog(body.limit)
        return {
            "processed": result.processed,
            "stopped_by_budget": result.stopped_by_budget,
        }

    @router.post("/internal/groups")
    async def internal_groups(
        request: Request,
        body: RegisterGroupRequest = _EMPTY_GROUP_BODY,
        key: InternalKey = None,
    ) -> dict[str, str]:
        """Register a served Telegram group (idempotent)."""
        context = await internal_context(request, key, resolve_context)
        if not body.chat_id:
            raise HTTPException(status_code=422, detail="chat_id required")
        space_id = await bind_telegram_group(
            context, body.chat_id, body.title, body.space_id
        )
        return {"status": "registered", "space_id": space_id}

    @router.post("/internal/review")
    async def internal_review(
        request: Request,
        key: InternalKey = None,
    ) -> dict[str, str]:
        """Return the human knowledge review report as markdown."""
        context = await internal_context(request, key, resolve_context)
        entries = await context.review.review()
        return {"report": render_review_report(entries)}

    return router
