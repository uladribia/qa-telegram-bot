# SPDX-License-Identifier: MIT
"""Telegram channel flow: update in, short status out.

This is the only place that knows how a Telegram update becomes an
application action. Everything below the dispatch is either a shared
application service or a Telegram-specific helper in this package, so the
canonical API can serve the same services without any of it.
"""

import logging
import time

from fastapi import HTTPException

from knowledge_bot.adapters.telegram.models import TelegramUpdate
from knowledge_bot.adapters.telegram.normalize import (
    normalize_callback,
    normalize_message,
)
from knowledge_bot.adapters.telegram.review import (
    deliver_review,
    escalate_overdue_reviews,
)
from knowledge_bot.application.answer_question import (
    addressed_answer_id,
    proactive_answer_id,
)
from knowledge_bot.application.feedback import (
    EDIT_PROMPT,
    PROPOSAL_ACK,
    REVIEW_REJECTED,
    callback_action,
    callback_target,
    proposal_prompt,
    render_review,
)
from knowledge_bot.application.intake import IntakeAction, decide_intake
from knowledge_bot.application.reviewers import (
    parse_reviewer_command,
    render_reviewer_list,
)
from knowledge_bot.domain.entities import (
    ChannelBinding,
    DeliveryReceipt,
    TelegramInteraction,
)
from knowledge_bot.domain.enums import BotMode, ReviewAction
from knowledge_bot.domain.errors import InvalidTransitionError
from knowledge_bot.domain.identity import principal_id, split_principal_id
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.infrastructure.logging import log_content
from knowledge_bot.models.messages import MemberChange, NormalizedMessage

ADMIN_APPROVED = "\u2705 Correcci\u00f3 aprovada."
REPORTER_THANKS = "Gr\u00e0cies! S'ha corregit la resposta."
INDEX_WARNING = (
    "Correcció aprovada. La indexació ha fallat i queda pendent de reparació."
)
REVIEWER_BOT_REFUSED = (
    "\u26a0\ufe0f No pots nomenar el bot com a revisor: respon al missatge "
    "d'una persona."
)
#: What a stranger is told. It has to be actionable on its own: the bot cannot
#: list a group's members, so the only way in is the person speaking there.
DM_ACCESS_NOTICE = (
    "\U0001f44b Encara no et puc associar a cap dels grups on participa aquest bot.\n"
    "Si formes part d'un d'aquests grups, envia-hi almenys un missatge i després "
    "torna'm a escriure per privat."
)
#: Receipt kind for the stranger notice, keyed per sender and per day.
DM_NOTICE_OBJECT_TYPE = "dm_access_notice"


def _reviewer_confirmed(scope: str, name: str) -> str:
    """Build the confirmation message after a reviewer nomination."""
    target = "revisor global" if scope == GLOBAL_SCOPE else "revisor d'aquest grup"
    return f"\u2705 {name} \u00e9s ara el {target}."


def _must_start_bot_alert(name: str) -> str:
    """Build the alert shown when a DM to a user could not be delivered."""
    return (
        f"{name}, abans has d'obrir un xat privat amb el bot "
        "(prem Start al seu perfil) i torna-ho a provar."
    )


def _log_inbound_message(
    message: NormalizedMessage,
    bot_mode: BotMode | None,
    *,
    served_spaces: int = 0,
) -> None:
    """Record the normalized message: who said what, where, and under which mode.

    The webhook request body already carries the raw update, so this is the
    connector's own view of the same event: the identity the core resolved, the
    space the message landed in, and the mode that decided its fate.

    Args:
        message: The normalized inbound message.
        bot_mode: The mode of the conversation, or ``None`` for a private chat.
        served_spaces: How many served groups a private asker belongs to.
    """
    log_content(
        "telegram_inbound_message",
        message_id=message.source_message_id,
        conversation_id=message.conversation_id,
        space_id=message.space_id,
        principal_id=message.principal_id,
        sender_name=message.sender_name,
        sender_user_id=message.sender_user_id,
        is_direct_message=message.is_direct_message,
        is_sender_allowed=message.is_sender_allowed,
        bot_mode=str(bot_mode) if bot_mode is not None else None,
        mentions_bot=message.mentions_bot,
        reply_to_message_id=message.reply_to_message_id,
        member_events=len(message.member_events),
        served_spaces=served_spaces,
        text=message.text,
    )


async def _observe_membership(
    context: AppContext, message: NormalizedMessage, binding: ChannelBinding
) -> int:
    """Record the membership changes this message reports, before any mode check.

    A silent or off group is still a group the bot belongs to, and knowing who
    is in it is how a member is later allowed to write privately. Doing this
    first is the point: a group set to ``off`` learns its members exactly like
    an active one.

    Args:
        context: The application context.
        message: The message carrying the membership events.
        binding: The binding of the conversation the message landed in.

    Returns:
        How many principals were recorded.
    """
    for event in message.member_events:
        if event.change is MemberChange.JOINED:
            await context.memberships.observe(event.principal_id, binding.space_id)
        else:
            await context.memberships.mark_left(event.principal_id, binding.space_id)
    return len(message.member_events)


async def _deliver_answer(
    context: AppContext,
    conversation_id: str,
    *,
    text: str,
    answer_id: str,
    also: tuple[str, ...] = (),
) -> bool:
    """Send one answer unless a receipt says it already went out.

    ``also`` carries the answers collapsed into this one. They are recorded
    against the same Telegram message, so a webhook retry re-sends only what
    is genuinely missing instead of repeating a group the reader already has.

    Args:
        context: The application context.
        conversation_id: The chat to answer in.
        text: The rendered answer to send.
        answer_id: The answer the feedback button will point at.
        also: Other answer ids this same message delivers.

    Returns:
        Whether this call sent it.
    """
    pending = [
        answer
        for answer in (answer_id, *also)
        if await context.delivery_receipts.get("answer", answer, "telegram") is None
    ]
    if not pending:
        return False
    message_id = await context.telegram.client.send_answer(
        conversation_id, text, answer_id
    )
    if message_id is None:
        raise HTTPException(status_code=503, detail="telegram delivery failed")
    for delivered in pending:
        await context.delivery_receipts.add(
            DeliveryReceipt(
                id=f"delivery:{delivered}:telegram",
                object_type="answer",
                object_id=delivered,
                channel="telegram",
                external_conversation_id=conversation_id,
                external_message_id=message_id,
                created_at=context.clock.now(),
            )
        )
    return True


async def handle_telegram_update(context: AppContext, update: TelegramUpdate) -> str:
    """Normalize one Telegram update and dispatch its channel flow."""
    started = time.perf_counter()
    log = logging.getLogger("knowledge_bot.webhook")
    log.info(
        "telegram_webhook_received",
        extra={"use_case": "telegram_webhook", "update_id": update.update_id},
    )
    try:
        await escalate_overdue_reviews(context)
        callback = normalize_callback(update)
        if callback is not None:
            result = await _handle_callback(
                context,
                callback.callback_id,
                callback.data,
                callback.sender_chat_id,
                callback.sender_name,
                callback.conversation_id,
            )
        else:
            message = normalize_message(update, context.telegram.identity)
            if message is None:
                result = "ignored"
            else:
                result = await _handle_message(context, message)
    except Exception:
        log.exception(
            "telegram_webhook_failed",
            extra={
                "use_case": "telegram_webhook",
                "update_id": update.update_id,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        raise
    log.info(
        "telegram_webhook_processed",
        extra={
            "use_case": "telegram_webhook",
            "update_id": update.update_id,
            "action": result,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return result


async def _handle_message(context: AppContext, message: NormalizedMessage) -> str:
    """Route a normalized message: resolve, control plane, then the channel flow.

    The order is the contract. A conversation the bot does not serve is not
    ours at all. Membership is recorded next, before the control plane and the
    mode, because who is in a group is a fact about the group and not a
    decision. The control plane itself is never gated by a mode: a reviewer
    command, a reply to a correction prompt, and a pending review are the bot
    keeping promises it already made, and turning a group off must not break a
    correction someone is in the middle of. Only then does the mode decide what
    happens to the question.
    """
    binding: ChannelBinding | None = None
    served: list[ChannelBinding] = []
    if message.is_direct_message:
        served = await _served_bindings(context, message)
        unknown = not message.is_sender_allowed and not served
        # Nobody the bot has seen in a group the operator still serves. Only a
        # reply to a prompt the bot itself sent is processed; anything else is
        # dropped before storage. A public bot username is discoverable, so an
        # open DM would let anyone spend the shared free AI quota and buzz the
        # admin with fake corrections.
        if unknown and not await _is_known_correction_reply(context, message):
            # A private mode of ``off`` means the bot is mute in private, and
            # that outranks explaining itself to a stranger.
            if context.settings.telegram_dm_bot_mode is BotMode.OFF:
                return "ignored"
            return await _notice_unknown_sender(context, message)
    else:
        binding = await context.spaces.resolve("telegram", message.conversation_id)
        if binding is None:
            return "ignored"
        message = message.model_copy(update={"space_id": binding.space_id})
        # Before anything else, including the control plane: knowing who is in
        # a served group is what later lets a member write to the bot privately,
        # and it must not depend on whether the message was a question.
        await _observe_membership(context, message, binding)
    _log_inbound_message(
        message,
        binding.bot_mode if binding is not None else None,
        served_spaces=len(served),
    )
    command_parts = (message.text or "").strip().split(maxsplit=1)
    if command_parts and command_parts[0].startswith("/reviewer"):
        return await _handle_reviewer_command(context, message)
    if message.reply_to_message_id is not None:
        handled = await _handle_feedback_reply(context, message)
        if handled:
            return handled
        # A reply to a prompt the bot already spent is not a new question. The
        # proposal behind it exists, so answering the text of the correction as
        # if it were a question would both confuse the reader and spend a
        # generation on it.
        if await _is_spent_correction_reply(context, message):
            return "ignored"
    if binding is not None:
        return await _handle_group_message(context, message, binding)
    return await _handle_direct_message(context, message, served)


async def _served_bindings(
    context: AppContext, message: NormalizedMessage
) -> list[ChannelBinding]:
    """Return the served groups this sender is an active member of."""
    if message.principal_id is None:
        return []
    return await context.memberships.served_bindings(message.principal_id, "telegram")


async def _observe_callback_membership(
    context: AppContext, sender_chat_id: str | None, group_chat_id: str | None
) -> None:
    """Record that someone pressed one of the bot's buttons in a served group.

    Pressing a button is evidence of membership just as a message is: the
    person was in the conversation, in front of the button, at that moment. It
    matters because the correction flow immediately moves to a private chat,
    and a member who has never typed in the group would otherwise be told by
    the bot it does not know them, right after it asked them a question.

    Args:
        context: The application context.
        sender_chat_id: The raw Telegram user id that pressed the button.
        group_chat_id: The conversation the button lives in.
    """
    if sender_chat_id is None or group_chat_id is None:
        return
    binding = await context.spaces.resolve("telegram", group_chat_id)
    if binding is None:
        return
    await context.memberships.observe(
        principal_id("telegram", sender_chat_id), binding.space_id
    )


async def _notice_unknown_sender(
    context: AppContext, message: NormalizedMessage
) -> str:
    """Tell someone the bot does not know them, at most once a day.

    Silence would leave a real member with no idea why the bot never answers
    them, but a public bot username is discoverable, so the notice is what
    keeps a stranger from turning it into a quota burner. One notice a day is
    the whole throttle: enough to explain, not enough to argue with.

    Args:
        context: The application context.
        message: The private message to answer.

    Returns:
        A short status string.
    """
    principal = message.principal_id or message.conversation_id
    notice_id = f"{principal}:{context.clock.now().strftime('%Y-%m-%d')}"
    prior = await context.delivery_receipts.get(
        DM_NOTICE_OBJECT_TYPE, notice_id, "telegram"
    )
    if prior is not None:
        return "ignored"
    message_id = await context.telegram.client.send_message(
        message.conversation_id, DM_ACCESS_NOTICE
    )
    if message_id is not None:
        await context.delivery_receipts.add(
            DeliveryReceipt(
                id=f"delivery:{DM_NOTICE_OBJECT_TYPE}:{notice_id}:telegram",
                object_type=DM_NOTICE_OBJECT_TYPE,
                object_id=notice_id,
                channel="telegram",
                external_conversation_id=message.conversation_id,
                external_message_id=message_id,
                created_at=context.clock.now(),
            )
        )
    return "dm_notice_sent"


async def _handle_group_message(
    context: AppContext, message: NormalizedMessage, binding: ChannelBinding
) -> str:
    """Apply the conversation's mode to one group message.

    Args:
        context: The application context.
        message: The message, already resolved to its space.
        binding: The binding that carries the mode to apply.

    Returns:
        A short status string.
    """
    action = decide_intake(message, binding.bot_mode)
    if action is IntakeAction.IGNORE:
        return "ignored"
    if action is IntakeAction.PROACTIVE:
        result = await context.listener.handle(message)
        if not result.confident_question:
            return result.status
        outcome = await context.proactive.respond(
            message,
            space_id=binding.space_id,
            answer_id=proactive_answer_id(message.id, binding.external_conversation_id),
        )
        if outcome.response is not None:
            await _deliver_answer(
                context,
                message.conversation_id,
                text=outcome.response.rendered_text,
                answer_id=outcome.response.answer_id,
            )
        return outcome.status
    if action is IntakeAction.INGEST:
        result = await context.listener.handle(message)
        return result.status
    await context.ingestor.ingest(message)
    response = await context.answer.answer_message(
        message,
        space_id=binding.space_id,
        answer_id=addressed_answer_id(message.id),
    )
    if response is not None:
        await _deliver_answer(
            context,
            message.conversation_id,
            text=response.rendered_text,
            answer_id=response.answer_id,
        )
    return "answer"


async def _handle_direct_message(
    context: AppContext,
    message: NormalizedMessage,
    served: list[ChannelBinding],
) -> str:
    """Apply the DM mode to one private message.

    A private message is never evidence: it is stored when the mode allows, but
    it never goes through the listener, because a conversation between two
    people is not knowledge about a community.

    Args:
        context: The application context.
        message: The private message.
        served: The served groups the sender belongs to, which are the scopes
            the question is answered in.

    Returns:
        A short status string.
    """
    mode = context.settings.telegram_dm_bot_mode
    if mode is BotMode.OFF:
        return "ignored"
    await context.ingestor.ingest(message)
    if mode is BotMode.SILENT:
        return "ingest"
    bundles = await context.direct_answers.bundles_for(message, served=served)
    if not bundles:
        return "ingest"
    delivered = 0
    for bundle in bundles:
        if await _deliver_answer(
            context,
            message.conversation_id,
            text=bundle.text,
            answer_id=bundle.representative_answer_id,
            also=bundle.answer_ids,
        ):
            delivered += 1
    logging.getLogger("knowledge_bot.webhook").info(
        "telegram_dm_answered",
        extra={
            "use_case": "telegram_dm",
            "answer_trigger": "dm",
            "principal_id": message.principal_id,
            "dm_scope_count": len(served),
            "dm_answer_count": len(bundles),
            "dm_delivered_count": delivered,
        },
    )
    return "dm_answer"


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
        await context.telegram.client.send_message(
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
        await context.telegram.client.send_message(
            chat, render_reviewer_list(reviewers)
        )
        return "reviewer_list"
    if action == "nominate":
        name = message.reply_to_user_name or "?"
        await context.reviewers.nominate(
            scope,
            principal_id("telegram", message.reply_to_user_id or ""),
            name,
            principal_id("telegram", message.sender_user_id or ""),
        )
        await context.telegram.client.send_message(
            chat, _reviewer_confirmed(scope, name)
        )
        return "reviewer_nominated"
    removed = await context.reviewers.remove(scope)
    await context.telegram.client.send_message(
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
    return await _pending_interaction(context, message) is not None


async def _is_spent_correction_reply(
    context: AppContext, message: NormalizedMessage
) -> bool:
    """Return whether a message answers a correction prompt already used.

    Args:
        context: The application context.
        message: The inbound message.

    Returns:
        ``True`` when the replied-to prompt exists and has been consumed.
    """
    if message.reply_to_message_id is None:
        return False
    interaction = await context.interactions.get(message.reply_to_message_id)
    return interaction is not None and interaction.consumed_at is not None


async def _pending_interaction(
    context: AppContext, message: NormalizedMessage
) -> TelegramInteraction | None:
    """Return the unused bot prompt this message replies to, if any."""
    reply_to = message.reply_to_message_id
    if reply_to is None:
        return None
    interaction = await context.interactions.get(reply_to)
    if interaction is None or interaction.consumed_at is not None:
        return None
    return interaction


async def _handle_feedback_reply(
    context: AppContext, message: NormalizedMessage
) -> str | None:
    """Handle a reply to a feedback prompt (reporter or reviewer)."""
    reply_to = message.reply_to_message_id
    if reply_to is None or message.text is None:
        return None
    if message.principal_id is None:
        return None
    interaction = await context.interactions.consume(
        reply_to, message.principal_id, context.clock.now()
    )
    if interaction is None:
        return None
    feedback = await context.feedback.get_feedback(interaction.object_id)
    if feedback is not None and interaction.interaction_type == "review_edit":
        review = await context.feedback.correction_request(feedback.id)
        if review is None or not await context.router.can_confirm(
            message.principal_id,
            review.origin_space_id,
            ReviewAction.EDIT,
        ):
            return None
        await context.feedback.admin_edit(feedback.id, message.text)
        destination = (
            f"telegram:{context.settings.admin_telegram_user_id}"
            if message.sender_is_admin
            else await context.router.destination(review.origin_space_id)
        )
        review = await context.feedback.correction_request(feedback.id)
        if destination is not None and review is not None:
            await deliver_review(
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
        feedback.id, message.text, message.principal_id, message.sender_name
    )
    if proposed is None:
        return None
    reporter_chat = (
        split_principal_id(proposed.reporter_principal_id)[1]
        if proposed.reporter_principal_id is not None
        else message.conversation_id
    )
    await context.telegram.client.send_message(reporter_chat, PROPOSAL_ACK)
    review = await context.feedback.correction_request(proposed.id)
    if review is not None:
        destination = await context.router.destination(review.origin_space_id)
        if destination is not None:
            await deliver_review(
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
    await _observe_callback_membership(context, reporter_chat_id, group_chat_id)
    action = callback_action(data)
    target = callback_target(data)
    if action is None:
        return "ignored"
    if target is None or reporter_chat_id is None:
        await context.telegram.client.answer_callback(
            callback_id, "No s'ha pogut processar aquesta acció."
        )
        return "ignored"
    if action == "start":
        # Proposing a correction is open to any group user.
        feedback = await context.feedback.start(
            target,
            principal_id("telegram", reporter_chat_id),
            reporter_name,
        )
        if feedback is None:
            await context.telegram.client.answer_callback(
                callback_id,
                "Aquesta resposta ja no es pot corregir.",
            )
            return "ignored"
        answer = await context.feedback.get_answer(target)
        prompt_id = await context.telegram.client.send_force_reply(
            reporter_chat_id or "",
            proposal_prompt(
                answer.question if answer else None,
                answer.answer if answer else "",
            ),
        )
        if prompt_id is not None:
            await context.interactions.add(
                TelegramInteraction(
                    external_message_id=prompt_id,
                    interaction_type="feedback_proposal",
                    object_id=feedback.id,
                    principal_id=principal_id("telegram", reporter_chat_id),
                    created_at=context.clock.now(),
                )
            )
            await context.telegram.client.answer_callback(callback_id)
            return "feedback_started"
        await context.telegram.client.answer_callback(
            callback_id,
            _must_start_bot_alert(reporter_name or ""),
        )
        username = context.telegram.identity.bot_username
        if group_chat_id is not None and username:
            await context.telegram.client.send_message(
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
    actor_principal_id = principal_id("telegram", reporter_chat_id)
    if review is None:
        await context.telegram.client.answer_callback(
            callback_id, "La revisió ja no existeix."
        )
        return "ignored"
    if not await context.router.can_confirm(
        actor_principal_id,
        review.origin_space_id,
        review_action,
    ):
        await context.telegram.client.answer_callback(
            callback_id, "No tens permís per fer aquesta acció."
        )
        return "ignored"
    if action == "approve_global" or action == "approve_group":
        try:
            version = await context.feedback.approve(target, review_action)
        except InvalidTransitionError:
            await context.telegram.client.answer_callback(
                callback_id, "Aquesta acció no té un espai d'origen."
            )
            return "ignored"
        if version is None:
            await context.telegram.client.answer_callback(
                callback_id, "La correcció ja està resolta."
            )
            return "ignored"
        attempt = await context.reindex.try_reindex_qa_version(version.id)
        if attempt.status == "failed":
            await context.telegram.client.send_message(
                reporter_chat_id or "", INDEX_WARNING
            )
        feedback = await context.feedback.get_feedback(target)
        if reporter_chat_id:
            await context.telegram.client.send_message(reporter_chat_id, ADMIN_APPROVED)
        if feedback is not None and feedback.reporter_principal_id is not None:
            channel, reporter_chat = split_principal_id(feedback.reporter_principal_id)
            if channel == "telegram":
                await context.telegram.client.send_message(
                    reporter_chat, REPORTER_THANKS
                )
        await context.telegram.client.answer_callback(callback_id)
        return "feedback_approved"
    if action == "edit":
        prompt = f"{EDIT_PROMPT}\n\nProposta actual:\n{review.proposed_answer}"
        prompt_id = await context.telegram.client.send_force_reply(
            reporter_chat_id or "", prompt
        )
        if prompt_id is not None:
            await context.interactions.add(
                TelegramInteraction(
                    external_message_id=prompt_id,
                    interaction_type="review_edit",
                    object_id=target,
                    principal_id=principal_id("telegram", reporter_chat_id),
                    created_at=context.clock.now(),
                )
            )
        await context.telegram.client.answer_callback(callback_id)
        return "feedback_edit"
    if action == "reject":
        await context.feedback.reject(target)
        if reporter_chat_id:
            await context.telegram.client.send_message(
                reporter_chat_id, REVIEW_REJECTED
            )
        await context.telegram.client.answer_callback(callback_id)
        return "feedback_rejected"
    return "ignored"
