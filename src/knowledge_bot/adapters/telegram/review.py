# SPDX-License-Identifier: MIT
"""Telegram delivery of correction reviews.

A review is a Telegram message with inline buttons, so both the delivery and
the escalation ladder that follows it are connector concerns rather than
application behaviour.
"""

from dataclasses import replace
from datetime import timedelta

from knowledge_bot.application.feedback import render_review
from knowledge_bot.domain.identity import split_principal_id
from knowledge_bot.infrastructure.context import AppContext


async def escalate_overdue_reviews(context: AppContext) -> None:
    """Route overdue reviewer deliveries to the configured admin."""
    now = context.clock.now()
    timeout = context.settings.reviewer_escalation_timeout_seconds
    admin = f"telegram:{context.settings.admin_telegram_user_id}"
    if context.settings.admin_telegram_user_id == "":
        return
    for feedback in await context.feedback.list_escalatable():
        failed_at = feedback.reviewer_delivery_failed_at
        if failed_at is None or now - failed_at < timedelta(seconds=timeout):
            continue
        review = await context.feedback.correction_request(feedback.id)
        if review is None:
            continue
        sent = await context.telegram.client.send_review(
            split_principal_id(admin)[1],
            render_review(review),
            feedback.id,
            include_global=True,
        )
        if sent is None:
            continue
        await context.feedback.save_feedback(
            replace(feedback, reviewer_escalated_at=now)
        )
        if review.origin_conversation_id is not None:
            await context.telegram.client.send_message(
                review.origin_conversation_id,
                "⏰ La revisió encara no ha rebut resposta. "
                f"Després de {timeout} s, l'admin ha rebut el cas.",
            )


async def deliver_review(
    context: AppContext,
    destination: str,
    text: str,
    feedback_id: str,
    origin_space_id: str | None,
    origin_conversation_id: str | None,
) -> bool:
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
    channel, chat_id = split_principal_id(destination)
    if channel != "telegram":
        return False
    sent = await context.telegram.client.send_review(
        chat_id,
        text,
        feedback_id,
        include_global=include_global,
    )
    if sent is not None:
        return True
    admin = context.settings.admin_telegram_user_id
    if destination != f"telegram:{context.settings.admin_telegram_user_id}":
        feedback = await context.feedback.get_feedback(feedback_id)
        if feedback is not None:
            await context.feedback.save_feedback(
                replace(
                    feedback,
                    reviewer_delivery_failed_at=context.clock.now(),
                    reviewer_destination=destination,
                )
            )
        timeout = context.settings.reviewer_escalation_timeout_seconds
        if admin:
            await context.telegram.client.send_message(
                context.settings.admin_telegram_user_id,
                "⚠️ El revisor no t\u00e9 disponible per privat. "
                f"La revisio s'escalarà a l'admin després de {timeout} s.",
            )
        if origin_conversation_id is not None:
            reviewer_name = await context.router.reviewer_name(
                destination, origin_space_id
            )
            await context.telegram.client.send_message(
                origin_conversation_id,
                f"⚠️ @{reviewer_name}, obre un xat privat amb el bot. "
                f"Si no hi ha resposta en {timeout} s, l'admin rebrà la revisió.",
            )
        if timeout == 0:
            await escalate_overdue_reviews(context)
    return False
