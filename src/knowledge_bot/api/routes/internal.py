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
from knowledge_bot.application.assessment import (
    BaselineAssessmentModel,
    build_answer_decision_request,
    build_proactive_question,
    parse_evidence_relevance,
    parse_relevance_decision,
    parse_sufficiency_decision,
)
from knowledge_bot.application.listener_pairing import select_pair
from knowledge_bot.application.review import render_review_report
from knowledge_bot.domain.enums import AiWorkClass
from knowledge_bot.domain.errors import (
    InvalidModelOutputError,
    ModelUnavailableError,
)
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.models.assessment import RetroevalCandidate
from knowledge_bot.models.operations import (
    BackgroundBacklogRequest,
    DailyReportRequest,
    EvalAnswerRequest,
    EvalDecisionCase,
    EvalDecisionRequest,
    IndexRepairRequest,
    PromoteRequest,
    ReindexRequest,
    RevertRequest,
    SeedRequest,
)
from knowledge_bot.ports.assessment import MessageAssessmentModel
from knowledge_bot.ports.system_one import SystemOneTransport

# Live-eval protection: one request may carry at most this many queries, and an
# isolate admits this many evaluation calls per minute. An unbounded burst of
# evaluation traffic can wedge the Worker; the budget guard does not protect
# against that.
_MAX_EVAL_QUERIES = 20
_EVAL_CALLS_PER_MINUTE = 60
_EMPTY_EVAL_BODY = Body(default_factory=EvalAnswerRequest)
_EMPTY_EVAL_DECISION_BODY = Body(default_factory=EvalDecisionRequest)
_EMPTY_REVERT_BODY = Body(default_factory=RevertRequest)
_EMPTY_PROMOTE_BODY = Body(default_factory=PromoteRequest)
_EMPTY_REINDEX_BODY = Body(default_factory=ReindexRequest)
_EMPTY_SEED_BODY = Body(default_factory=SeedRequest)
_EMPTY_BACKLOG_BODY = Body(default_factory=BackgroundBacklogRequest)
_EMPTY_REPAIR_BODY = Body(default_factory=IndexRepairRequest)
_EMPTY_GROUP_BODY = Body(default_factory=RegisterGroupRequest)
_EMPTY_DAILY_REPORT_BODY = Body(default_factory=DailyReportRequest)


def _evaluation_model(context: AppContext) -> MessageAssessmentModel:
    """Return the evaluation-only decision model, or refuse the request.

    Args:
        context: The application context.

    Returns:
        A decision model that is never reachable from user traffic.

    Raises:
        HTTPException: 503 when this runtime cannot reach a decision model.
    """
    factory = context.decision_evaluator
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail="this runtime has no decision evaluator configured",
        )
    return factory()


def _transport_for(assessment: MessageAssessmentModel) -> SystemOneTransport:
    """Return the transport behind a System-One assessment model.

    Only the decision backend has one; the baseline has no transport, and a
    post-retrieval case is meaningless for it because nothing decides there yet.
    """
    transport = getattr(assessment, "transport", None)
    if not isinstance(transport, SystemOneTransport):
        raise HTTPException(
            status_code=422,
            detail="post-retrieval decisions require the clef-flash backend",
        )
    return transport


def _model_name(assessment: MessageAssessmentModel) -> str:
    """Return the model name a System-One assessment model uses."""
    return str(getattr(assessment, "model", ""))


async def _decide_answer_case(
    context: AppContext,
    case: EvalDecisionCase,
    transport: SystemOneTransport,
    model: str,
    backend: str,
    sufficiency_floor: float,
) -> dict[str, object]:
    """Decide one post-retrieval case: sufficiency, selection and trigger.

    The answer path needs two decisions over the same shortlist — is anything
    here enough, and which items answer the question — and the listener path
    can ask in the same request whether the message is an open question worth
    answering. All of it travels in one request, because the state is sent once
    and each extra question costs far less than another round trip.
    """
    settings = context.settings
    started = time.perf_counter()
    evidence = tuple((item.evidence_id, item.text) for item in case.evidence)
    question = case.question or case.text
    state, questions = build_answer_decision_request(question, evidence)
    if case.ask_proactive:
        questions["open_question"] = build_proactive_question()
    try:
        payload = await transport.decide(model=model, state=state, questions=questions)
        sufficiency = parse_sufficiency_decision(payload)
        relevance = parse_evidence_relevance(
            payload, tuple(evidence_id for evidence_id, _ in evidence)
        )
        proactive = _optional_noul(payload, "open_question")
    except (InvalidModelOutputError, ModelUnavailableError) as error:
        return {
            "case_id": case.case_id,
            "backend": backend,
            "error": type(error).__name__,
            "detail": error.args[0] if error.args else "",
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    selected = sorted(
        (
            name
            for name, score in relevance.items()
            if score >= settings.retroeval_relevance_threshold
        ),
        key=lambda name: relevance[name],
        reverse=True,
    )
    return {
        "case_id": case.case_id,
        "backend": backend,
        "sufficiency": sufficiency,
        "evidence_relevance": relevance,
        "selected": selected if sufficiency >= sufficiency_floor else [],
        "proactive": proactive,
        "evidence_count": len(evidence),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _optional_noul(payload: dict[str, object], decision: str) -> float | None:
    """Return one yes/no probability, or ``None`` when it was not asked."""
    try:
        return parse_relevance_decision(payload, (decision,))[decision]
    except InvalidModelOutputError:
        return None


async def _decide_one(
    context: AppContext,
    assessment: MessageAssessmentModel,
    backend: str,
    case: EvalDecisionCase,
    candidates: tuple[RetroevalCandidate, ...],
) -> dict[str, object]:
    """Decide one evaluated case and report the outcome or the failure.

    Args:
        context: The application context, for the pairing policy's thresholds.
        assessment: The backend deciding this case.
        backend: The requested backend, echoed in the result.
        case: The message and its open questions.
        candidates: The open questions, as domain values.

    Returns:
        The decisions for this case, or an error describing the failure.
    """
    started = time.perf_counter()
    try:
        decided = await assessment.assess(case.text, candidates=candidates)
    except (InvalidModelOutputError, ModelUnavailableError) as error:
        return {
            "case_id": case.case_id,
            "backend": backend,
            "error": type(error).__name__,
            "detail": error.args[0] if error.args else "",
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    selected = select_pair(
        decided,
        candidates,
        relevance_threshold=context.settings.retroeval_relevance_threshold,
        relevance_margin=context.settings.retroeval_relevance_margin,
    )
    classification = decided.classification
    return {
        "case_id": case.case_id,
        "backend": backend,
        "intent_probabilities": {
            "question": classification.scores.question,
            "knowledge_update": classification.scores.knowledge_update,
            "correction": classification.scores.correction,
            "chitchat": classification.scores.chitchat,
        },
        "best_label": classification.best_label.value,
        "best_score": classification.best_score,
        "margin": classification.margin,
        "prefiltered": classification.prefiltered,
        "relevance": decided.pair_relevance,
        "selected_candidate_id": selected.candidate_id if selected else None,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


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
            space_id=body.space_id,
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

    @router.post("/internal/eval/decision")
    async def internal_eval_decision(
        request: Request,
        body: EvalDecisionRequest = _EMPTY_EVAL_DECISION_BODY,
        key: InternalKey = None,
    ) -> dict[str, object]:
        """Decide messages with an alternative backend, changing nothing.

        This is the measurement surface for the decision layer: it answers with
        either the deployed baseline or the evaluation-only decision model, and
        it persists nothing — no message, no pair, no projection, no delivery,
        no bot state. A failure of the decision model is reported per case as
        an error; it is never answered from the baseline, because an evaluation
        that silently substitutes another model measures the wrong thing.

        Each message becomes at most one request to the decision model, carrying
        its intent plus every candidate relevance, and the pairing decision is
        taken here by the application's own policy.
        """
        context = await internal_context(request, key, resolve_context)
        if not body.cases:
            raise HTTPException(status_code=422, detail="at least one case required")
        _admit_eval_call()
        await _require_evaluation_budget(context)
        assessment = (
            BaselineAssessmentModel(context.classifier)
            if body.backend == "baseline"
            else _evaluation_model(context)
        )
        results: list[dict[str, object]] = []
        for case in body.cases:
            candidates = tuple(
                RetroevalCandidate(
                    candidate_id=item.candidate_id,
                    question_message_id=item.question_message_id or item.candidate_id,
                    question=item.question,
                    relation=item.relation,
                )
                for item in case.candidates
            )
            if case.question or case.evidence:
                results.append(
                    await _decide_answer_case(
                        context,
                        case,
                        _transport_for(assessment),
                        _model_name(assessment),
                        body.backend,
                        context.settings.answer_similarity_floor,
                    )
                )
            else:
                results.append(
                    await _decide_one(
                        context, assessment, body.backend, case, candidates
                    )
                )
        return {"backend": body.backend, "results": results}

    @router.get("/internal/budget")
    async def internal_budget(
        request: Request, key: InternalKey = None
    ) -> dict[str, object]:
        """Report today's estimated AI spend and what it still allows.

        The guard is invisible by design: when it stops the bot, the bot simply
        says nothing. That is correct for a user and useless to an operator
        asking why a proactive group went quiet, and the only other way to find
        out is to query the meter by hand. Read-only, and the numbers are the
        same estimate the guard itself uses.
        """
        context = await internal_context(request, key, resolve_context)
        spend = await context.budget.spend()
        return {
            "day": context.budget.day(),
            "neurons": round(spend.neurons, 1),
            "limit": spend.limit,
            "proactive_ceiling": spend.proactive_ceiling,
            "background_ceiling": spend.background_ceiling,
            "proactive_allowed": await context.budget.work_allowed(
                AiWorkClass.PROACTIVE
            ),
            "background_allowed": await context.budget.work_allowed(
                AiWorkClass.BACKGROUND
            ),
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
        """Register a served Telegram group (idempotent).

        Omitted fields keep what is already registered, so this is also how a
        group's mode is changed.
        """
        context = await internal_context(request, key, resolve_context)
        if not body.chat_id:
            raise HTTPException(status_code=422, detail="chat_id required")
        space_id = await bind_telegram_group(
            context, body.chat_id, body.title, body.space_id, body.bot_mode
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
