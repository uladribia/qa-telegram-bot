# SPDX-License-Identifier: MIT
"""FastAPI application factory and HTTP routes.

The Worker bindings are only available per request (in ``request.scope["env"]``),
so the app resolves its context through a callable rather than at import time.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, cast

from fastapi import Body, FastAPI, Header, HTTPException, Request

from knowledge_bot.adapters.http.api_routes import build_api_router
from knowledge_bot.adapters.inbound.telegram import (
    TELEGRAM_RUNTIME_SOURCE_ID,
    normalize_callback,
    normalize_message,
)
from knowledge_bot.adapters.telegram.routes import register_telegram_routes
from knowledge_bot.application.classifier import QUESTION, IntentScores
from knowledge_bot.application.feedback import (
    EDIT_PROMPT,
    GROUP_SCOPE,
    PROPOSAL_ACK,
    REVIEW_REJECTED,
    CorrectionRequest,
    callback_action,
    callback_target,
    proposal_prompt,
    render_review,
)
from knowledge_bot.application.intake import IntakeAction, decide_intake
from knowledge_bot.application.review import render_review_report
from knowledge_bot.application.reviewers import (
    parse_reviewer_command,
    render_reviewer_list,
)
from knowledge_bot.contracts.api import (
    BackgroundBacklogRequest,
    DailyReportRequest,
    EvalAnswerRequest,
    RegisterGroupRequest,
    ReindexRequest,
    RevertRequest,
    SeedRequest,
)
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.telegram import TelegramUpdate
from knowledge_bot.domain.entities import ReviewerEvent, TelegramInteraction
from knowledge_bot.domain.enums import (
    AiWorkClass,
    AnswerMode,
    ClassificationStatus,
    IndexStatus,
    ReviewAction,
)
from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.domain.identity import principal_id
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.infrastructure.composition import AppContext
from knowledge_bot.infrastructure.logging import configure_logging
from knowledge_bot.infrastructure.security import secrets_match

ContextResolver = Callable[[Request], AppContext | Awaitable[AppContext]]


async def _resolved_context(
    resolve_context: ContextResolver, request: Request
) -> AppContext:
    """Resolve either the Worker or local asynchronous context."""
    context = resolve_context(request)
    if isinstance(context, Awaitable):
        return await cast(Awaitable[AppContext], context)
    return cast(AppContext, context)


ADMIN_APPROVED = "\u2705 Correcci\u00f3 aprovada."
REPORTER_THANKS = "Gr\u00e0cies! S'ha corregit la resposta."

# Longest parent question text stored on a matched pair reply.
_MAX_LISTENER_QUESTION_CHARS = 500
_EMPTY_EVAL_BODY = Body(default_factory=EvalAnswerRequest)
_EMPTY_REVERT_BODY = Body(default_factory=RevertRequest)
_EMPTY_REINDEX_BODY = Body(default_factory=ReindexRequest)
_EMPTY_SEED_BODY = Body(default_factory=SeedRequest)
_EMPTY_BACKLOG_BODY = Body(default_factory=BackgroundBacklogRequest)
_EMPTY_GROUP_BODY = Body(default_factory=RegisterGroupRequest)
_EMPTY_DAILY_REPORT_BODY = Body(default_factory=DailyReportRequest)


def _reviewer_confirmed(scope: str, name: str) -> str:
    """Build the confirmation message after a reviewer nomination."""
    target = "revisor global" if scope == GLOBAL_SCOPE else "revisor d'aquest grup"
    return f"\u2705 {name} \u00e9s ara el {target}."


REVIEWER_BOT_REFUSED = (
    "\u26a0\ufe0f No pots nomenar el bot com a revisor: respon al missatge "
    "d'una persona."
)


def _must_start_bot_alert(name: str) -> str:
    """Build the alert shown when a DM to a user could not be delivered."""
    return (
        f"{name}, abans has d'obrir un xat privat amb el bot "
        "(prem Start al seu perfil) i torna-ho a provar."
    )


async def _deliver_review(
    context: AppContext,
    destination: str,
    text: str,
    feedback_id: str,
    origin_space_id: str | None,
    origin_conversation_id: str | None,
) -> None:
    """Send a review to its reviewer, or back to the admin on failure.

    Args:
        context: The application context.
        destination: The reviewer's private chat id.
        text: The rendered review.
        feedback_id: The correction under review.
        origin_space_id: Logical space used for reviewer lookup.
        origin_conversation_id: Conversation used for the group activation notice.
    """
    include_global = await context.router.can_approve_global(destination)
    sent = await context.transport.send_review(
        destination,
        text,
        feedback_id,
        include_global=include_global,
    )
    admin = context.settings.admin_telegram_user_id
    if sent is None and destination != admin:
        if admin:
            await context.transport.send_message(
                admin,
                "\u26a0\ufe0f El revisor no t\u00e9 encara disponible per privat. "
                "La correcció continua pendent; no l'has d'aprovar en el seu lloc.",
            )
        if origin_conversation_id is not None:
            reviewer_name = await context.router.reviewer_name(
                destination, origin_space_id
            )
            await context.transport.send_message(
                origin_conversation_id,
                f"\u26a0\ufe0f @{reviewer_name}, obre un xat privat amb el bot "
                "per activar la revisio. La correccio continua pendent.",
            )


async def _handle_telegram_update(context: AppContext, update: TelegramUpdate) -> str:
    """Normalize one Telegram update and dispatch its channel flow."""
    callback = normalize_callback(update)
    if callback is not None:
        return await _handle_callback(
            context,
            callback.callback_id,
            callback.data,
            callback.sender_chat_id,
            callback.sender_name,
            callback.conversation_id,
        )
    message = normalize_message(update, context.identity)
    if message is None:
        return "ignored"
    message = await _resolve_message_space(context, message)
    if message is None:
        return "ignored"
    return await _handle_message(context, message)


def create_app(resolve_context: ContextResolver) -> FastAPI:
    """Build the FastAPI application.

    Args:
        resolve_context: Returns the application context for a request.

    Returns:
        The configured FastAPI app.
    """
    configure_logging()
    app = FastAPI(title="knowledge-bot")
    app.include_router(build_api_router(resolve_context))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Report Worker liveness."""
        return {"status": "ok"}

    register_telegram_routes(app, resolve_context, _handle_telegram_update)

    @app.post("/internal/eval/answer")
    async def internal_eval_answer(
        request: Request,
        body: EvalAnswerRequest = _EMPTY_EVAL_BODY,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, object]:
        """Answer a question without sending it, for live answer evals.

        Returns the decided mode, rendered answer, citations, and retrieved
        evidence ids for explicitly authorized deterministic evaluations. No
        message ever reaches Telegram.
        """
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        if not body.question:
            raise HTTPException(status_code=422, detail="question required")
        await _require_evaluation_budget(context)
        question = body.question
        try:
            preview = await context.answer.dry_run(question)
        except ModelUnavailableError:
            return {
                "question": question,
                "mode": AnswerMode.UNAVAILABLE.value,
                "answer": "model unavailable",
                "text": "",
                "source_ids": [],
                "evidence_ids": [],
                "citations": [],
            }
        outcome = preview.outcome
        return {
            "question": question,
            "mode": outcome.mode.value,
            "answer": outcome.answer,
            "text": outcome.text,
            "source_ids": outcome.source_ids,
            "evidence_ids": [item.source_id for item in preview.evidence],
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

    @app.post("/internal/recap")
    async def internal_recap(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Force the opportunistic admin recap check from an external scheduler."""
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        sent = await context.recap.maybe_send()
        return {"status": "sent" if sent else "skipped"}

    @app.post("/internal/jobs/daily-report")
    async def internal_daily_report(
        request: Request,
        body: DailyReportRequest = _EMPTY_DAILY_REPORT_BODY,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Run the same deterministic report job as the scheduled handler."""
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        sent = await context.daily_report.run(force=body.force)
        return {"status": "sent" if sent else "skipped"}

    @app.post("/internal/report")
    async def internal_report(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Force the opportunistic admin report check from a scheduler."""
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        sent = await context.reviewer_report.maybe_send()
        return {"status": "sent" if sent else "skipped"}

    @app.post("/internal/revert")
    async def internal_revert(
        request: Request,
        body: RevertRequest = _EMPTY_REVERT_BODY,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Revert a Q&A item to the version its current one superseded.

        The only rollback path: a CLI operation, never a Telegram action.
        """
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        if not body.qa_item_id:
            raise HTTPException(status_code=422, detail="qa_item_id required")
        restored = await context.reverter.revert(body.qa_item_id)
        if restored is None:
            raise HTTPException(status_code=404, detail="nothing to revert")
        await context.reindex.reindex_qa_version(restored.id)
        return {"status": "reverted", "restored_version_id": restored.id}

    @app.post("/internal/reindex")
    async def internal_reindex(
        request: Request,
        body: ReindexRequest = _EMPTY_REINDEX_BODY,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, object]:
        """Rebuild part of the derived vector store from D1.

        Accepts an optional JSON body with ``qa_after``/``msg_after`` cursors
        and ``limit``; without it, one unbounded pass indexes everything.
        """
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        await _require_evaluation_budget(context)
        if not body.model_fields_set:
            report = await context.reindex.rebuild()
        else:
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

    @app.post("/internal/retrieve")
    async def internal_retrieve(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, dict[str, list[str]]]:
        """Return the retrieved source ids for each query (eval support)."""
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        await _require_evaluation_budget(context)
        payload = await request.json()
        queries = payload.get("queries", []) if isinstance(payload, dict) else []
        results: dict[str, list[str]] = {}
        for query in queries:
            retrieved = await context.answer.retrieval.retrieve(str(query))
            results[str(query)] = [
                item.anchor or item.source_id for item in retrieved.qa
            ]
        return {"results": results}

    @app.post("/internal/seed")
    async def internal_seed(
        request: Request,
        body: SeedRequest = _EMPTY_SEED_BODY,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, int]:
        """Seed Q&A entries and imported messages into D1."""
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        created, skipped, renewed, diverged, version_ids = await context.seed.seed_qa(
            body.qa, body.scope, body.renew
        )
        indexed = 0
        for version_id in version_ids:
            if await context.reindex.reindex_qa_version(version_id):
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

    @app.post("/internal/background/process-backlog")
    async def internal_process_background_backlog(
        request: Request,
        body: BackgroundBacklogRequest = _EMPTY_BACKLOG_BODY,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, int]:
        """Process a bounded batch of budget-deferred background messages."""
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        if not await context.budget.work_allowed(AiWorkClass.MAINTENANCE):
            raise HTTPException(status_code=429, detail="maintenance budget exhausted")
        processed = await context.background_indexer.process_backlog(body.limit)
        return {"processed": processed}

    @app.post("/internal/groups")
    async def internal_groups(
        request: Request,
        body: RegisterGroupRequest = _EMPTY_GROUP_BODY,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Register a served Telegram group (idempotent)."""
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        if not body.chat_id:
            raise HTTPException(status_code=422, detail="chat_id required")
        space_id = await context.spaces.bind(
            channel="telegram",
            external_conversation_id=body.chat_id,
            conversation_id=body.chat_id,
            space_id=body.space_id,
            title=body.title,
            source_id=TELEGRAM_RUNTIME_SOURCE_ID,
            source_kind="telegram",
            source_authority=40,
        )
        return {"status": "registered", "space_id": space_id}

    @app.post("/internal/review")
    async def internal_review(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Return the human knowledge review report as markdown."""
        context = await _resolved_context(resolve_context, request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        entries = await context.review.review()
        return {"report": render_review_report(entries)}

    return app


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


async def _resolve_message_space(
    context: AppContext, message: NormalizedMessage
) -> NormalizedMessage | None:
    """Resolve a group conversation to its logical space.

    Direct chats keep no space binding; they remain private user conversations.
    """
    if message.is_direct_message:
        return message
    binding = await context.spaces.resolve("telegram", message.conversation_id)
    if binding is None:
        return None
    return message.model_copy(update={"space_id": binding.space_id})


async def _handle_background_message(
    context: AppContext, message: NormalizedMessage
) -> str:
    """Classify and ingest an unaddressed group message.

    Only clear-cut chitchat is discarded; everything else is kept as
    context, and a reply that answers a parent question is matched into a
    question-answer pair. Media-only messages carry no text to score, so
    they are kept unlabeled.

    Args:
        context: The application context.
        message: The unaddressed inbound message.

    Returns:
        A short status string.
    """
    text = (message.text or "").strip()
    if not text:
        await context.ingestor.ingest(
            message,
            classification_status=ClassificationStatus.NO_TEXT,
            index_status=IndexStatus.NOT_ELIGIBLE,
        )
        return "ingest"
    if not await context.budget.work_allowed(AiWorkClass.BACKGROUND):
        await context.ingestor.ingest(
            message,
            classification_status=ClassificationStatus.DEFERRED_BUDGET,
            index_status=IndexStatus.NOT_INDEXED,
        )
        return "ingest"
    classification = await context.classifier.classify(text)
    scores = classification.scores
    label = classification.best_label.value
    score = classification.best_score
    context_question = await _match_parent_question(context, message, scores)
    status = (
        ClassificationStatus.PREFILTER_CHITCHAT
        if not classification.embedding
        else ClassificationStatus.CLASSIFIED
    )
    result = await context.ingestor.ingest(
        message,
        intent_label=label,
        intent_score=score,
        context_question=context_question,
        classification_status=status,
        intent_scores={
            "question": scores.question,
            "knowledge_update": scores.knowledge_update,
            "correction": scores.correction,
            "chitchat": scores.chitchat,
        },
        index_status=IndexStatus.NOT_ELIGIBLE,
    )
    if result.created:
        await context.background_indexer.process(message.id, classification.embedding)
    return "ingest_pair" if context_question is not None else "ingest"


async def _match_parent_question(
    context: AppContext, message: NormalizedMessage, scores: IntentScores
) -> str | None:
    """Return the parent question text when a reply answers a question.

    Args:
        context: The application context.
        message: The reply being ingested.
        scores: The reply's intent scores.

    Returns:
        The parent question text, or ``None`` when this is no clear pair.
    """
    if message.reply_to_message_id is None:
        return None
    if not context.classifier.is_answer_like(scores):
        return None
    parent = await context.ingestor.messages.get(
        f"{message.conversation_id}:{message.reply_to_message_id}"
    )
    if parent is None or not parent.text:
        return None
    if parent.intent_label != QUESTION:
        return None
    if (parent.intent_score or 0.0) < context.classifier.question_match_threshold:
        return None
    return parent.text[:_MAX_LISTENER_QUESTION_CHARS]


async def _handle_message(context: AppContext, message: NormalizedMessage) -> str:
    """Route a normalized message: reviewer commands, feedback replies, intake."""
    if (
        message.is_direct_message
        and not message.is_sender_allowed
        and not await _is_known_correction_reply(context, message)
    ):
        # A stranger's DM. Only a reply to a prompt the bot itself sent is
        # processed; anything else is dropped before storage. A public bot
        # username is discoverable, so an open DM would let anyone spend the
        # shared free AI quota and buzz the admin with fake corrections.
        return "ignored"
    command_parts = (message.text or "").strip().split(maxsplit=1)
    if command_parts and command_parts[0].startswith("/reviewer"):
        return await _handle_reviewer_command(context, message)
    if message.reply_to_message_id is not None:
        handled = await _handle_feedback_reply(context, message)
        if handled:
            return handled
    action = decide_intake(
        message,
        background_listener_enabled=context.settings.background_listener_enabled,
    )
    if action is IntakeAction.IGNORE:
        return "ignored"
    if action is IntakeAction.INGEST:
        return await _handle_background_message(context, message)
    result = await context.ingestor.ingest(message)
    if action is IntakeAction.ANSWER and result.created:
        await context.answer.answer(message)
    return "answer" if action is IntakeAction.ANSWER else "ingest"
    return action.value


async def _handle_reviewer_command(
    context: AppContext, message: NormalizedMessage
) -> str:
    """Handle ``/reviewer``: nominate, remove, or list correction reviewers.

    Only the admin may change reviewers. A nomination points at the person the
    command replies to (their Telegram user id); without a reply the command
    lists the current reviewers. Without ``global`` the scope is the group the
    command was typed in.

    Args:
        context: The application context.
        message: The command message.

    Returns:
        A short status string.
    """
    if not message.sender_is_admin:
        return "ignored"
    if message.is_reply_to_bot:
        await context.transport.send_message(
            message.conversation_id, REVIEWER_BOT_REFUSED
        )
        return "reviewer_bot_refused"
    action, is_global = parse_reviewer_command(message.text or "")
    if not is_global and message.space_id is None:
        return "ignored"
    scope = GLOBAL_SCOPE if is_global else scope_for_space(message.space_id or "")
    chat = message.conversation_id
    if action == "nominate" and message.reply_to_user_id is None:
        reviewers = await context.reviewers.list_reviewers()
        await context.transport.send_message(chat, render_reviewer_list(reviewers))
        return "reviewer_list"
    if action == "nominate":
        name = message.reply_to_user_name or "?"
        await context.reviewers.nominate(
            scope,
            message.reply_to_user_id or "",
            name,
            message.sender_user_id or "",
        )
        await context.transport.send_message(chat, _reviewer_confirmed(scope, name))
        return "reviewer_nominated"
    removed = await context.reviewers.remove(scope)
    await context.transport.send_message(
        chat,
        "\u2705 Revisor eliminat."
        if removed
        else "No hi havia cap revisor per eliminar.",
    )
    return "reviewer_removed"


async def _is_known_correction_reply(
    context: AppContext, message: NormalizedMessage
) -> bool:
    """Return whether a message answers a correction prompt we sent.

    Args:
        context: The application context.
        message: The inbound private message.

    Returns:
        ``True`` when the message replies to a proposal or edit prompt.
    """
    reply_to = message.reply_to_message_id
    if reply_to is None:
        return False
    interaction = await context.telegram_interactions.get(reply_to)
    return interaction is not None and interaction.consumed_at is None


async def _handle_feedback_reply(
    context: AppContext, message: NormalizedMessage
) -> str | None:
    """Handle a reply to a feedback prompt (reporter or reviewer)."""
    reply_to = message.reply_to_message_id
    if reply_to is None or message.text is None:
        return None
    interaction = await context.telegram_interactions.consume(
        reply_to, context.clock.now()
    )
    if interaction is None:
        return None
    if (
        interaction.principal_id is not None
        and interaction.principal_id != message.principal_id
    ):
        return None
    feedback = await context.feedback_repo.get(interaction.object_id)
    if feedback is not None and interaction.interaction_type == "review_edit":
        review = await context.feedback.correction_request(feedback.id)
        if review is None or not await context.router.can_confirm(
            message.sender_user_id,
            review.origin_space_id,
            ReviewAction.EDIT,
        ):
            return None
        await context.feedback.admin_edit(feedback.id, message.text)
        destination = await context.router.destination(review.origin_space_id)
        review = await context.feedback.correction_request(feedback.id)
        if destination is not None and review is not None:
            await _deliver_review(
                context,
                destination,
                render_review(review),
                feedback.id,
                review.origin_space_id,
                review.origin_conversation_id,
            )
        return "reviewer_edited"
    if feedback is None or interaction.interaction_type != "feedback_proposal":
        return None
    proposed = await context.feedback.propose(
        feedback.id, message.text, message.sender_name
    )
    if proposed is None:
        return None
    reporter_chat = proposed.reporter_chat_id or message.conversation_id
    await context.transport.send_message(reporter_chat, PROPOSAL_ACK)
    review = await context.feedback.correction_request(proposed.id)
    if review is not None:
        destination = await context.router.destination(review.origin_space_id)
        if destination is not None:
            await _deliver_review(
                context,
                destination,
                render_review(review),
                proposed.id,
                review.origin_space_id,
                review.origin_conversation_id,
            )
    return "proposed"


async def _handle_callback(
    context: AppContext,
    callback_id: str,
    data: str | None,
    reporter_chat_id: str | None,
    reporter_name: str | None = None,
    group_chat_id: str | None = None,
) -> str:
    """Route an inline-button press through the correction flow.

    Args:
        context: The application context.
        callback_id: The callback to acknowledge.
        data: The callback payload.
        reporter_chat_id: The private chat to prompt for the proposal.
        reporter_name: The reporter's display name, cited as the author.
        group_chat_id: The chat the button lives in, for fallback notices.

    Returns:
        A short status string.
    """
    action = callback_action(data)
    target = callback_target(data)
    if action is None or target is None or reporter_chat_id is None:
        return "ignored"
    if action == "start":
        # Proposing a correction is open to any group user.
        feedback = await context.feedback.start(
            target,
            principal_id("telegram", reporter_chat_id),
            reporter_chat_id,
            reporter_name,
        )
        if feedback is None:
            return "ignored"
        answer = await context.feedback.answers.get(target)
        prompt_id = await context.transport.send_force_reply(
            reporter_chat_id or "",
            proposal_prompt(
                answer.question if answer else None,
                answer.answer if answer else "",
            ),
        )
        if prompt_id is not None:
            await context.telegram_interactions.add(
                TelegramInteraction(
                    external_message_id=prompt_id,
                    interaction_type="feedback_proposal",
                    object_id=feedback.id,
                    principal_id=principal_id("telegram", reporter_chat_id),
                    created_at=context.clock.now(),
                )
            )
            await context.transport.answer_callback(callback_id)
            return "feedback_started"
        await context.transport.answer_callback(
            callback_id,
            _must_start_bot_alert(reporter_name or ""),
        )
        username = context.identity.bot_username
        if group_chat_id is not None and username:
            await context.transport.send_message(
                group_chat_id,
                "\u26a0\ufe0f Per corregir, obre primer un xat privat amb el bot: "
                f"https://t.me/{username} "
                "(despr\u00e9s torna a pr\u00e9mer el bot\u00f3).",
            )
        return "feedback_prompt_undelivered"
    # Confirming a correction is only for its reviewer (the group's reviewer,
    # the global reviewer, or the admin), enforced here on the server.
    review_action = {
        "approve_global": ReviewAction.APPROVE_GLOBAL,
        "approve_group": ReviewAction.APPROVE_LOCAL,
        "edit": ReviewAction.EDIT,
        "reject": ReviewAction.REJECT,
    }.get(action)
    if review_action is None:
        return "ignored"
    review = await context.feedback.correction_request(target)
    if review is None or not await context.router.can_confirm(
        reporter_chat_id,
        review.origin_space_id,
        review_action,
    ):
        return "ignored"
    if action == "approve_global" or action == "approve_group":
        scope = GLOBAL_SCOPE if action == "approve_global" else GROUP_SCOPE
        version = await context.feedback.approve(target, scope)
        if version is None:
            return "ignored"
        await context.reindex.reindex_qa_version(version.id)
        feedback = await context.feedback_repo.get(target)
        await context.reviewer_report.record(
            _reviewer_event(
                target,
                review,
                "edited_approved"
                if feedback is not None and feedback.admin_edited_answer
                else "approved",
                GLOBAL_SCOPE
                if action == "approve_global"
                else scope_for_space(review.origin_space_id or ""),
                reporter_chat_id,
                reporter_name,
                context.clock.now(),
            )
        )
        if reporter_chat_id:
            await context.transport.send_message(reporter_chat_id, ADMIN_APPROVED)
        if feedback is not None and feedback.reporter_chat_id:
            await context.transport.send_message(
                feedback.reporter_chat_id, REPORTER_THANKS
            )
        await context.transport.answer_callback(callback_id)
        return "feedback_approved"
    if action == "edit":
        prompt = f"{EDIT_PROMPT}\n\nProposta actual:\n{review.proposed_answer}"
        prompt_id = await context.transport.send_force_reply(
            reporter_chat_id or "", prompt
        )
        if prompt_id is not None:
            await context.telegram_interactions.add(
                TelegramInteraction(
                    external_message_id=prompt_id,
                    interaction_type="review_edit",
                    object_id=target,
                    principal_id=principal_id("telegram", reporter_chat_id),
                    created_at=context.clock.now(),
                )
            )
        await context.transport.answer_callback(callback_id)
        return "feedback_edit"
    if action == "reject":
        await context.feedback.reject(target)
        await context.reviewer_report.record(
            _reviewer_event(
                target,
                review,
                "rejected",
                None,
                reporter_chat_id,
                reporter_name,
                context.clock.now(),
            )
        )
        if reporter_chat_id:
            await context.transport.send_message(reporter_chat_id, REVIEW_REJECTED)
        await context.transport.answer_callback(callback_id)
        return "feedback_rejected"
    return "ignored"


def _reviewer_event(
    feedback_id: str,
    review: CorrectionRequest,
    action: str,
    approval_scope: str | None,
    reviewer_user_id: str | None,
    reviewer_name: str | None,
    created_at: datetime,
) -> ReviewerEvent:
    """Build the report event for a reviewer's resolution."""
    return ReviewerEvent(
        feedback_id=feedback_id,
        action=action,
        created_at=created_at,
        reviewer_user_id=reviewer_user_id,
        reviewer_name=reviewer_name,
        group_label=review.group_label,
        question=review.question,
        approval_scope=approval_scope,
    )
