# SPDX-License-Identifier: MIT
"""FastAPI application factory and HTTP routes.

The Worker bindings are only available per request (in ``request.scope["env"]``),
so the app resolves its context through a callable rather than at import time.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request

from knowledge_bot.adapters.inbound.telegram import (
    is_valid_webhook_secret,
    normalize_callback,
    normalize_message,
)
from knowledge_bot.application.feedback import (
    EDIT_PROMPT,
    GROUP_SCOPE,
    PROPOSAL_ACK,
    PROPOSAL_PROMPT,
    REVIEW_REJECTED,
    CorrectionRequest,
    callback_action,
    callback_target,
    render_review,
)
from knowledge_bot.application.intake import IntakeAction, decide_intake
from knowledge_bot.application.review import render_review_report
from knowledge_bot.application.reviewers import (
    parse_reviewer_command,
    render_reviewer_list,
)
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.contracts.telegram import TelegramUpdate
from knowledge_bot.domain.entities import ReviewerEvent
from knowledge_bot.domain.enums import AnswerMode
from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.domain.scope import GLOBAL_SCOPE
from knowledge_bot.infrastructure.composition import AppContext
from knowledge_bot.infrastructure.logging import configure_logging
from knowledge_bot.infrastructure.security import secrets_match

ContextResolver = Callable[[Request], AppContext]

ADMIN_APPROVED = "\u2705 Correcci\u00f3 aprovada."
REPORTER_THANKS = "Gr\u00e0cies! S'ha corregit la resposta."


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
    context: AppContext, destination: str, text: str, feedback_id: str
) -> None:
    """Send a review to its reviewer and tell the admin if that fails.

    Args:
        context: The application context.
        destination: The reviewer's private chat id.
        text: The rendered review.
        feedback_id: The correction under review.
    """
    sent = await context.transport.send_review(destination, text, feedback_id)
    admin = context.settings.admin_telegram_user_id
    if sent is None and admin and destination != admin:
        await context.transport.send_message(
            admin,
            "\u26a0\ufe0f No he pogut enviar una correcci\u00f3 al revisor per DM: "
            "ha d'obrir primer un xat privat amb el bot.",
        )


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
                context,
                callback.callback_id,
                callback.data,
                callback.sender_chat_id,
                callback.sender_name,
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

        Returns the decided mode, the rendered answer, the citations (with the
        evidence text, so the judge can run as a separate, conditional call),
        and the retrieved evidence ids. No message ever reaches Telegram.
        """
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        await _require_evaluation_budget(context)
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

    @app.post("/internal/eval/judge")
    async def internal_eval_judge(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Judge one answer against its cited evidence.

        The eval harness calls this only for cases that already passed the
        deterministic checks, halving the judge's share of the AI quota.
        """
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        await _require_evaluation_budget(context)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        evidence = payload.get("evidence") or []
        try:
            verdict = await context.answer.judge(
                str(payload.get("question", "")),
                str(payload.get("answer", "")),
                [str(item) for item in evidence],
            )
        except ModelUnavailableError:
            return {"verdict": "error", "reason": "model unavailable"}
        return {"verdict": verdict.verdict, "reason": verdict.reason}

    @app.post("/internal/recap")
    async def internal_recap(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Force the opportunistic admin recap check from an external scheduler."""
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        sent = await context.recap.maybe_send()
        return {"status": "sent" if sent else "skipped"}

    @app.post("/internal/report")
    async def internal_report(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Force the opportunistic admin report check from a scheduler."""
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        sent = await context.reviewer_report.maybe_send()
        return {"status": "sent" if sent else "skipped"}

    @app.post("/internal/revert")
    async def internal_revert(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Revert a Q&A item to the version its current one superseded.

        The only rollback path: a CLI operation, never a Telegram action.
        """
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        payload = await request.json()
        qa_item_id = (
            str(payload.get("qa_item_id", "")) if isinstance(payload, dict) else ""
        )
        restored = await context.reverter.revert(qa_item_id)
        if restored is None:
            raise HTTPException(status_code=404, detail="nothing to revert")
        await context.reindex.reindex_qa_version(restored.id)
        return {"status": "reverted", "restored_version_id": restored.id}

    @app.post("/internal/reindex")
    async def internal_reindex(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, object]:
        """Rebuild part of the derived vector store from D1.

        Accepts an optional JSON body with ``qa_after``/``msg_after`` cursors
        and ``limit``; without it, one unbounded pass indexes everything.
        """
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        await _require_evaluation_budget(context)
        payload = await request.json()
        body = payload if isinstance(payload, dict) else {}
        report = await context.reindex.reindex(
            qa_after=body.get("qa_after"),
            msg_after=body.get("msg_after"),
            limit=int(body["limit"]) if body.get("limit") is not None else None,
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
        context = resolve_context(request)
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
        scope = payload.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            scope = "global"
        renew = bool(payload.get("renew", False))
        created, skipped, renewed, version_ids = await context.seed.seed_qa(
            qa_entries, scope, renew
        )
        indexed = 0
        for version_id in version_ids:
            if await context.reindex.reindex_qa_version(version_id):
                indexed += 1
        message_count = await context.seed.seed_messages(messages, scope)
        return {
            "qa": created,
            "qa_skipped": skipped,
            "indexed": indexed,
            "qa_renewed": renewed,
            "messages": message_count,
        }

    @app.post("/internal/groups")
    async def internal_groups(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Register a served Telegram group (idempotent)."""
        context = resolve_context(request)
        if not secrets_match(key, context.settings.internal_admin_key):
            raise HTTPException(status_code=401, detail="invalid key")
        payload = await request.json()
        if not isinstance(payload, dict) or not str(payload.get("chat_id", "")).strip():
            raise HTTPException(status_code=400, detail="chat_id required")
        title = payload.get("title")
        await context.groups.register(
            str(payload["chat_id"]).strip(),
            title=str(title).strip()
            if isinstance(title, str) and title.strip()
            else None,
        )
        return {"status": "registered"}

    @app.post("/internal/review")
    async def internal_review(
        request: Request,
        key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
    ) -> dict[str, str]:
        """Return the human knowledge review report as markdown."""
        context = resolve_context(request)
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
    if message.text is not None and message.text.split()[0].startswith("/reviewer"):
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
    result = await context.ingestor.ingest(message)
    if action is IntakeAction.ANSWER and result.created:
        await context.answer.answer(message)
    await context.recap.maybe_send()
    await context.reviewer_report.maybe_send()
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
    scope = GLOBAL_SCOPE if is_global else message.conversation_id
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
    if await context.feedback_repo.find_by_proposal_prompt(reply_to) is not None:
        return True
    return await context.feedback_repo.find_by_edit_prompt(reply_to) is not None


async def _handle_feedback_reply(
    context: AppContext, message: NormalizedMessage
) -> str | None:
    """Handle a reply to a feedback prompt (reporter or reviewer)."""
    reply_to = message.reply_to_message_id
    if reply_to is None or message.text is None:
        return None
    feedback = await context.feedback_repo.find_by_edit_prompt(reply_to)
    if feedback is not None:
        review = await context.feedback.correction_request(feedback.id)
        if review is None or not await context.router.can_confirm(
            message.sender_user_id, review.group_chat_id
        ):
            return None
        await context.feedback.admin_edit(feedback.id, message.text)
        destination = await context.router.destination(review.group_chat_id)
        review = await context.feedback.correction_request(feedback.id)
        if destination is not None and review is not None:
            await _deliver_review(
                context, destination, render_review(review), feedback.id
            )
        return "reviewer_edited"
    feedback = await context.feedback_repo.find_by_proposal_prompt(reply_to)
    if feedback is None:
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
        destination = await context.router.destination(review.group_chat_id)
        if destination is not None:
            await _deliver_review(
                context, destination, render_review(review), proposed.id
            )
    return "proposed"


async def _handle_callback(
    context: AppContext,
    callback_id: str,
    data: str | None,
    reporter_chat_id: str | None,
    reporter_name: str | None = None,
) -> str:
    """Route an inline-button press through the correction flow.

    Args:
        context: The application context.
        callback_id: The callback to acknowledge.
        data: The callback payload.
        reporter_chat_id: The private chat to prompt for the proposal.
        reporter_name: The reporter's display name, cited as the author.

    Returns:
        A short status string.
    """
    action = callback_action(data)
    target = callback_target(data)
    if action is None or target is None:
        return "ignored"
    if action == "start":
        # Proposing a correction is open to any group user.
        feedback = await context.feedback.start(
            target, None, reporter_chat_id, reporter_name
        )
        if feedback is None:
            return "ignored"
        prompt_id = await context.transport.send_force_reply(
            reporter_chat_id or "", PROPOSAL_PROMPT
        )
        if prompt_id is not None:
            await context.feedback_repo.save(
                replace(feedback, proposal_prompt_message_id=prompt_id)
            )
        else:
            await context.transport.answer_callback(
                callback_id,
                _must_start_bot_alert(reporter_name or ""),
            )
            return "feedback_prompt_undelivered"
        await context.transport.answer_callback(callback_id)
        return "feedback_started"
    # Confirming a correction is only for its reviewer (the group's reviewer,
    # the global reviewer, or the admin), enforced here on the server.
    review = await context.feedback.correction_request(target)
    if review is None or not await context.router.can_confirm(
        reporter_chat_id, review.group_chat_id
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
                GLOBAL_SCOPE if action == "approve_global" else review.group_chat_id,
                reporter_chat_id,
                reporter_name,
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
            feedback = await context.feedback_repo.get(target)
            if feedback is not None:
                await context.feedback_repo.save(
                    replace(feedback, edit_prompt_message_id=prompt_id)
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
) -> ReviewerEvent:
    """Build the report event for a reviewer's resolution."""
    return ReviewerEvent(
        feedback_id=feedback_id,
        action=action,
        created_at=datetime.now(UTC),
        reviewer_user_id=reviewer_user_id,
        reviewer_name=reviewer_name,
        group_label=review.group_label,
        question=review.question,
        approval_scope=approval_scope,
    )
