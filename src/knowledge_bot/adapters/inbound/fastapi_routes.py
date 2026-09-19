# SPDX-License-Identifier: MIT
"""FastAPI application factory and HTTP routes.

The Worker bindings are only available per request (in ``request.scope["env"]``),
so the app resolves its context through a callable rather than at import time.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request

from knowledge_bot.adapters.inbound.telegram import (
    is_valid_webhook_secret,
    normalize_callback,
    normalize_message,
)
from knowledge_bot.application.feedback import (
    EDIT_PROMPT,
    PROPOSAL_ACK,
    PROPOSAL_PROMPT,
    REVIEW_REJECTED,
    callback_action,
    callback_target,
    render_review,
)
from knowledge_bot.application.intake import IntakeAction, decide_intake
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.contracts.telegram import TelegramUpdate
from knowledge_bot.domain.enums import AnswerMode
from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.infrastructure.composition import AppContext
from knowledge_bot.infrastructure.logging import configure_logging
from knowledge_bot.infrastructure.security import secrets_match

ContextResolver = Callable[[Request], AppContext]

ADMIN_APPROVED = "\u2705 Correcci\u00f3 aprovada."
REPORTER_THANKS = "Gr\u00e0cies! S'ha corregit la resposta."


def create_app(resolve_context: ContextResolver) -> FastAPI:
    """Build the FastAPI application.

    Args:
        resolve_context: Returns the application context for a request.

    Returns:
        The configured FastAPI app.
    """
    configure_logging()
    app = FastAPI(title="knowledge-bot")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Report Worker liveness."""
        return {"status": "ok"}

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        secret: Annotated[
            str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")
        ] = None,
    ) -> dict[str, str]:
        """Receive Telegram updates and route them to the right flow."""
        context = resolve_context(request)
        if not is_valid_webhook_secret(
            secret, context.settings.telegram_webhook_secret
        ):
            raise HTTPException(status_code=401, detail="invalid secret")
        update = TelegramUpdate.model_validate(await request.json())
        callback = normalize_callback(update)
        if callback is not None:
            status = await _handle_callback(
                context, callback.callback_id, callback.data, callback.sender_chat_id
            )
            return {"status": status}
        message = normalize_message(update, context.identity)
        if message is None:
            return {"status": "ignored"}
        return {"status": await _handle_message(context, message)}

    @app.post("/internal/eval/answer")
    async def internal_eval_answer(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, object]:
        """Answer a question without sending it, for live answer evals.

        Returns the decided mode, the rendered answer, the citations, and an
        optional judge verdict. No message ever reaches Telegram.
        """
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        payload = await request.json()
        question = str(payload.get("question", ""))
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
                "judge": None,
            }
        outcome = preview.outcome
        verdict: dict[str, str] | None = None
        if payload.get("judge") and outcome.mode is not AnswerMode.ABSTENTION:
            judged = await context.answer.judge(
                question,
                outcome.answer,
                [item.text for item in preview.evidence],
            )
            verdict = {"verdict": judged.verdict, "reason": judged.reason}
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
                }
                for item in preview.evidence
                if item.source_id in outcome.source_ids
            ],
            "judge": verdict,
        }

    @app.post("/internal/recap")
    async def internal_recap(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Force the opportunistic recap check from an external scheduler."""
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        sent = await context.recap.maybe_send(context.settings.allowed_telegram_chat_id)
        return {"status": "sent" if sent else "skipped"}

    @app.post("/internal/reindex")
    async def internal_reindex(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, int]:
        """Rebuild the derived vector store from D1."""
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        report = await context.reindex.reindex()
        return {"qa": report.qa, "messages": report.messages}

    @app.post("/internal/retrieve")
    async def internal_retrieve(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, dict[str, list[str]]]:
        """Return the retrieved source ids for each query (eval support)."""
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
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
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, int]:
        """Seed Q&A entries and imported messages into D1."""
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        qa_entries = [SeedQA.model_validate(item) for item in payload.get("qa") or []]
        messages = [
            NormalizedMessage.model_validate(item)
            for item in payload.get("messages") or []
        ]
        created, skipped = await context.seed.seed_qa(qa_entries)
        message_count = await context.seed.seed_messages(messages)
        return {"qa": created, "qa_skipped": skipped, "messages": message_count}

    return app


async def _handle_message(context: AppContext, message: NormalizedMessage) -> str:
    """Route a normalized message: feedback replies first, then intake."""
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
    result = await context.ingestor.ingest(message)
    if action is IntakeAction.ANSWER and result.created:
        await context.answer.answer(message)
    await context.recap.maybe_send(message.conversation_id)
    return action.value


async def _handle_feedback_reply(
    context: AppContext, message: NormalizedMessage
) -> str | None:
    """Handle a reply to a feedback prompt (reporter or admin)."""
    reply_to = message.reply_to_message_id
    if reply_to is None or message.text is None:
        return None
    admin_id = context.settings.admin_telegram_user_id
    feedback = await context.feedback.find_by_edit_prompt(reply_to)
    if feedback is not None and message.sender_is_admin:
        await context.feedback.admin_edit(feedback.id, message.text)
        review = await context.feedback.correction_request(feedback.id)
        if review is not None:
            await context.transport.send_review(
                admin_id, render_review(review), feedback.id
            )
        return "admin_edited"
    feedback = await context.feedback.find_by_proposal_prompt(reply_to)
    if feedback is None:
        return None
    proposed = await context.feedback.propose(feedback.id, message.text)
    if proposed is None:
        return None
    reporter_chat = proposed.reporter_chat_id or message.conversation_id
    await context.transport.send_message(reporter_chat, PROPOSAL_ACK)
    review = await context.feedback.correction_request(proposed.id)
    if review is not None:
        await context.transport.send_review(
            admin_id, render_review(review), proposed.id
        )
    return "proposed"


async def _handle_callback(
    context: AppContext,
    callback_id: str,
    data: str | None,
    reporter_chat_id: str | None,
) -> str:
    """Route an inline-button press through the correction flow.

    Args:
        context: The application context.
        callback_id: The callback to acknowledge.
        data: The callback payload.
        reporter_chat_id: The private chat to prompt for the proposal.

    Returns:
        A short status string.
    """
    action = callback_action(data)
    target = callback_target(data)
    if action is None or target is None:
        return "ignored"
    admin_id = context.settings.admin_telegram_user_id
    is_admin = reporter_chat_id == admin_id
    # Proposing a correction is open to any group user; confirming it is not.
    if action != "start" and not is_admin:
        return "ignored"
    if action == "start":
        feedback = await context.feedback.start(target, None, reporter_chat_id)
        if feedback is None:
            return "ignored"
        prompt_id = await context.transport.send_force_reply(
            reporter_chat_id or "", PROPOSAL_PROMPT
        )
        if prompt_id is not None:
            await context.feedback.set_proposal_prompt(feedback.id, prompt_id)
        await context.transport.answer_callback(callback_id)
        return "feedback_started"
    if action == "approve":
        version = await context.feedback.approve(target)
        if version is None:
            return "ignored"
        await context.reindex.reindex()
        feedback = await context.feedback.get(target)
        await context.transport.send_message(admin_id, ADMIN_APPROVED)
        if feedback is not None and feedback.reporter_chat_id:
            await context.transport.send_message(
                feedback.reporter_chat_id, REPORTER_THANKS
            )
        await context.transport.answer_callback(callback_id)
        return "feedback_approved"
    if action == "edit":
        review = await context.feedback.correction_request(target)
        if review is None:
            return "ignored"
        prompt = f"{EDIT_PROMPT}\n\nProposta actual:\n{review.proposed_answer}"
        prompt_id = await context.transport.send_force_reply(admin_id, prompt)
        if prompt_id is not None:
            await context.feedback.set_edit_prompt(target, prompt_id)
        await context.transport.answer_callback(callback_id)
        return "feedback_edit"
    if action == "reject":
        await context.feedback.reject(target)
        await context.transport.send_message(admin_id, REVIEW_REJECTED)
        await context.transport.answer_callback(callback_id)
        return "feedback_rejected"
    return "ignored"
