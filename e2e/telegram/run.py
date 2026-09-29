# SPDX-License-Identifier: MIT

"""The real-Telegram E2E scenario runner.

One sequential, stateful, black-box scenario against the deployed Worker over
real Telegram (Telethon acting as the existing human admin account). Not a
pytest suite: run it with `ALLOW_CLOUDFLARE_LIVE_TESTS=1 make test-e2e-telegram`.
"""

import asyncio
import contextlib
import sys
import time
import uuid
from collections.abc import Sequence
from typing import cast

from telethon import utils

from e2e.telegram.client import E2ERuntimeError, Message, TelegramE2EClient
from e2e.telegram.config import (
    BASELINE_TOKEN,
    COLLAPSE_QUESTION,
    COLLAPSE_TOKEN,
    DEFERRED_REPLY_GRACE_SECONDS,
    E2E_TITLE_PREFIX,
    LISTENER_QUIET_SECONDS,
    PROJECTION_SETTLE_SECONDS,
    PROJECTION_WAIT_SECONDS,
    SCOPE_GLOBAL,
    SENTINEL_QUESTION,
    SPACE_A,
    SPACE_B,
    E2EConfigError,
    TelegramE2ESettings,
)
from e2e.telegram.fixtures import (
    InternalWorker,
    qa_item_id_for,
    reset_and_seed_baseline,
)

# The listener step re-asks its follow-up while the paired evidence settles.
LISTENER_MAX_ATTEMPTS = 3

# Visible button labels (never callback payloads).
BTN_FLAG = "⚠️ Està malament?"
BTN_EDIT = "✏️ Editar"
BTN_REJECT = "❌ Rebutjar"
BTN_APPROVE_GROUP = "👥 Aprovar grup"
BTN_APPROVE_GLOBAL = "🌐 Aprovar global"

# Exact bot-facing strings asserted by the scenario.
ABSTENTION = "No tinc prou informació fiable per respondre-ho."
FONTS_LABEL = "Fonts:"
PROPOSAL_PROMPT = "Què corregiries?"
PROPOSAL_ACK = "Ho he enviat a revisió"
EDIT_PROMPT = "Envia'm el text correcte."
REVIEW_REJECTED = "❌ Correcció rebutjada."
APPROVED = "✅ Correcció aprovada."
REVIEWER_REMOVED = "Revisor eliminat"
REVIEWER_ABSENT = "cap revisor per eliminar"

# Quiet window proving a multi-scope private answer that should collapse to one
# delivery does not in fact send a second, label-free duplicate.
DM_DEDUP_QUIET_SECONDS = 12

# A collapsed private answer carries no scope heading; a label here would mean
# the scopes were not collapsed.
GLOBAL_LABEL = "\U0001f310 Global"
GROUP_LABEL_MARK = "\U0001f465"

# Every served group goes back to this after a run. It is the production
# default, so a run that fails to reset leaves the groups exactly as it found
# them rather than in a state only the harness understands.
DEFAULT_GROUP_MODE = "active"
REVIEWER_CONFIRMED = "revisor d'aquest grup"
REVIEWER_LIST = "Revisors:"
REVIEW_HEADER = "Correcció proposada"
REPORT_TITLE = "📊 Resum diari"
REPORT_SECTIONS = (
    "Preguntes adreçades:",
    "Preguntes de fons:",
    "Correccions:",
    "IA:",
)

NOT_ADMIN_DETAIL = (
    "The Telethon user is not the deployed ADMIN_TELEGRAM_USER_ID. "
    "Use the existing admin Telegram account for this suite."
)


class E2EFailure(RuntimeError):
    """Raised when one scenario step fails; ends the run after cleanup."""


class Scenario:
    """State for one sequential E2E run.

    Args:
        settings: Validated E2E settings.
        client: The connected Telethon helper.

    Attributes:
        settings: The validated settings.
        client: The Telethon helper.
        run_id: The short random token making this run unique.
    """

    def __init__(
        self, settings: TelegramE2ESettings, client: TelegramE2EClient
    ) -> None:
        """Create the scenario without touching Telegram yet."""
        self.settings = settings
        self.client = client
        self.run_id = uuid.uuid4().hex[:8]
        self.worker = InternalWorker(settings)
        self.bot: object | None = None
        self.group_a: object | None = None
        self.group_b: object | None = None
        self._dm_last_seen = 0
        self._mention_answer: Message | None = None
        self._second_review: Message | None = None

    async def close(self) -> None:
        """Close the internal HTTP worker."""
        await self.worker.close()

    # ------------------------------------------------------------- utilities

    def _require(self, chat: object | None, name: str) -> object:
        """Return a resolved chat or fail the scenario.

        Args:
            chat: The resolved chat entity, possibly `None`.
            name: The internal name for the error message.

        Returns:
            The non-null chat entity.

        Raises:
            E2EFailure: If the chat was never resolved.
        """
        if chat is None:
            raise E2EFailure(f"internal error: {name} was not resolved")
        return chat

    async def _last_message_id(self, chat: object) -> int:
        """Return the newest message id in a chat right now.

        Args:
            chat: The resolved chat entity.

        Returns:
            The newest message id, or `0` for an empty chat.
        """
        fetched = await self.client.raw.get_messages(chat, limit=1)
        first = cast(Sequence[Message | None], fetched)[0]
        return int(first.id) if first is not None else 0

    async def wait_dm(
        self,
        *,
        after_id: int | None = None,
        needle: str | None = None,
        timeout: int | None = None,
    ) -> Message:
        """Wait for a new bot message in the bot DM.

        Args:
            after_id: Only messages strictly newer than this id count;
                defaults to the newest DM id the scenario has seen.
            needle: Optional substring the text must contain.
            timeout: Optional explicit timeout.

        Returns:
            The matching bot DM message.

        Raises:
            E2ERuntimeError: If no matching message appears in time.
        """
        bot = self._require(self.bot, "bot")
        return await self.client.wait_for_bot_message(
            bot,
            after_id=self._dm_last_seen if after_id is None else after_id,
            predicate=(lambda text: needle in text) if needle is not None else None,
            timeout=timeout,
        )

    async def ask_and_expect(
        self,
        chat: object,
        text: str,
        needle: str,
        *,
        after_id: int,
        reply_to: int | None = None,
    ) -> Message:
        """Send a message to a chat and wait for the bot answer with a needle.

        Args:
            chat: The resolved chat entity.
            text: The message to send.
            needle: The substring the answer must contain.
            after_id: The newest message id before sending.
            reply_to: Optional message id to reply to.

        Returns:
            The matching bot answer.

        Raises:
            E2ERuntimeError: If no matching answer appears in time.
        """
        await self.client.send(chat, text, reply_to=reply_to)
        return await self.client.wait_for_bot_message(
            chat,
            after_id=after_id,
            predicate=lambda answer: needle in answer,
        )

    async def ask_and_expect_soft(
        self,
        chat: object,
        text: str,
        needle: str,
        *,
        after_id: int,
        reply_to: int | None = None,
    ) -> Message | None:
        """Like `ask_and_expect`, but return `None` on a timeout.

        Args:
            chat: The resolved chat entity.
            text: The message to send.
            needle: The substring the answer must contain.
            after_id: The newest message id before sending.
            reply_to: Optional message id to reply to.

        Returns:
            The matching bot answer, or `None` when nothing matched.
        """
        await self.client.send(chat, text, reply_to=reply_to)
        try:
            return await self.client.wait_for_bot_message(
                chat,
                after_id=after_id,
                predicate=lambda answer: needle in answer,
            )
        except E2ERuntimeError:
            return None

    async def ask_until(
        self,
        chat: object,
        text: str,
        needle: str,
        *,
        attempts: int = 3,
        gap_seconds: int = PROJECTION_WAIT_SECONDS,
        reply_to: int | None = None,
    ) -> Message:
        """Ask, retrying while a fresh projection settles into Vectorize.

        An approval (or a reset) re-projects a Q&A version; Vectorize reads are
        eventually consistent, so the first question right after an approval
        may still be answered from the previous text. Each attempt sends the
        question again and waits for an answer carrying the needle.

        Args:
            chat: The resolved chat entity.
            text: The message to send.
            needle: The substring the answer must contain.
            attempts: How many ask attempts to make.
            gap_seconds: Seconds between attempts.
            reply_to: Optional message id to reply to.

        Returns:
            The matching bot answer.

        Raises:
            E2EFailure: If no attempt produced a matching answer.
        """
        for attempt in range(attempts):
            if attempt > 0:
                await asyncio.sleep(gap_seconds)
            answer = await self.ask_and_expect_soft(
                chat,
                text,
                needle,
                after_id=await self._last_message_id(chat),
            )
            if answer is not None:
                return answer
        raise E2EFailure(
            "answer_not_settled",
            f"no answer containing {needle!r} after {attempts} attempts",
        )

    async def _last_own_message(self, chat: object) -> Message:
        """Return the newest message authored by the human in a chat.

        Args:
            chat: The resolved chat entity.

        Returns:
            The newest outgoing (human) message.

        Raises:
            E2EFailure: If no own message is among the recent ones.
        """
        fetched = await self.client.raw.get_messages(chat, limit=5)
        for message in cast(Sequence[Message | None], fetched):
            if message is not None and getattr(message, "out", False):
                return message
        raise E2EFailure(
            "own_message_missing", "could not find the harness's own message"
        )

    async def _assert_has_button(self, message: Message, label: str) -> None:
        """Fail unless the message carries a button labelled exactly.

        Args:
            message: The bot message to inspect.
            label: The exact visible label.

        Raises:
            E2EFailure: If the label is absent.
        """
        rows = message.buttons or []
        for row in rows:
            for button in row:
                if getattr(button, "text", None) == label:
                    return
        detail = f"message {message.id} has no {label!r} button"
        raise E2EFailure("button_missing", detail)

    # ----------------------------------------------------------------- steps

    async def preflight(self) -> None:
        """Validate config, resolve entities, bind groups, reset and seed."""
        settings = self.settings
        me = await self.client.get_me()
        if getattr(me, "bot", False):
            raise E2EFailure(
                "not_a_human", "the Telethon session must be a human account"
            )
        bot_username = settings.telegram_e2e_bot_username
        self.bot = await self.client.resolve_bot(bot_username)
        title_a = settings.telegram_e2e_group_a_title
        title_b = settings.telegram_e2e_group_b_title
        for title in (title_a, title_b):
            if not title.startswith(E2E_TITLE_PREFIX):
                detail = f"group title {title!r} lacks the {E2E_TITLE_PREFIX} guard"
                raise E2EFailure("group_guard", detail)
        self.group_a = await self.client.group_a()
        self.group_b = await self.client.group_b()

        chat_id_a = str(utils.get_peer_id(self.group_a))
        chat_id_b = str(utils.get_peer_id(self.group_b))
        await self.worker.register_group(
            chat_id_a, title_a, SPACE_A, bot_mode=DEFAULT_GROUP_MODE
        )
        await self.worker.register_group(
            chat_id_b, title_b, SPACE_B, bot_mode=DEFAULT_GROUP_MODE
        )

        # Open the private channel robustly: tolerate a fresh /start that
        # produces no visible bot reply (the bot was already started).
        await self.client.send(self.bot, "/start")
        # Tolerate a fresh /start that produces no visible bot reply.
        with contextlib.suppress(E2ERuntimeError):
            await self.wait_dm(timeout=DEFERRED_REPLY_GRACE_SECONDS)
        self._dm_last_seen = await self._last_message_id(self.bot)

        await reset_and_seed_baseline(settings)
        # The reset's re-projection must be visible before the first question:
        # Vectorize reads are eventually consistent.
        await asyncio.sleep(PROJECTION_SETTLE_SECONDS)

    async def addressing(self) -> None:
        """Exercise mention, /ask, reply-to-bot and DM paths."""
        bot_username = self.settings.telegram_e2e_bot_username

        before = await self._last_message_id(self.group_a)
        mention = await self.ask_and_expect(
            self.group_a,
            f"@{bot_username} {SENTINEL_QUESTION}",
            BASELINE_TOKEN,
            after_id=before,
        )
        if FONTS_LABEL not in mention.text:
            raise E2EFailure("fonts_missing", "mention answer has no 'Fonts:' block")
        await self._assert_has_button(mention, BTN_FLAG)
        self._mention_answer = mention

        before = await self._last_message_id(self.group_b)
        await self.ask_and_expect(
            self.group_b,
            f"/ask {SENTINEL_QUESTION}",
            BASELINE_TOKEN,
            after_id=before,
        )

        before = await self._last_message_id(self.group_a)
        await self.ask_and_expect(
            self.group_a,
            SENTINEL_QUESTION,
            BASELINE_TOKEN,
            after_id=before,
            reply_to=self._mention_answer.id,
        )

        before = self._dm_last_seen
        dm_answer = await self.ask_and_expect(
            self.bot, SENTINEL_QUESTION, BASELINE_TOKEN, after_id=before
        )
        self._dm_last_seen = max(self._dm_last_seen, dm_answer.id)

    async def abstention(self) -> None:
        """Ask an unknown question in B and require the exact abstention."""
        bot_username = self.settings.telegram_e2e_bot_username
        before = await self._last_message_id(self.group_b)
        unknown = f"E2E-UNKNOWN-{self.run_id}"
        question = f"@{bot_username} Quin és el planeta secret {unknown}?"
        await self.ask_and_expect(self.group_b, question, ABSTENTION, after_id=before)

    async def listener(self) -> None:
        """Store an unaddressed question, pair an explicit reply, and retrieve."""
        question = f"Prova LISTENER-{self.run_id}: a quina hora és l'activitat?"
        before = await self._last_message_id(self.group_a)
        await self.client.send(self.group_a, question)
        listener_question = await self._last_own_message(self.group_a)

        await self.client.assert_no_bot_message(
            self.group_a, after_id=before, seconds=LISTENER_QUIET_SECONDS
        )

        reply = f"L'activitat de la prova LISTENER-{self.run_id} és a les 17:42."
        await self.client.send(self.group_a, reply, reply_to=listener_question.id)

        bot_username = self.settings.telegram_e2e_bot_username
        follow_up = (
            f"@{bot_username} A quina hora és l'activitat de la prova "
            f"LISTENER-{self.run_id}?"
        )
        answer = await self.ask_until(
            self.group_a, follow_up, "17:42", attempts=LISTENER_MAX_ATTEMPTS
        )
        token = f"LISTENER-{self.run_id}"
        if token not in answer.text:
            print(f"  note: generator dropped the {token} token")

    async def reviewer_setup(self) -> None:
        """Nominate the human admin as local reviewer in both groups."""
        for chat in (self.group_a, self.group_b):
            anchor = await self._last_own_message(chat)
            before = await self._last_message_id(chat)
            await self.client.send(chat, "/reviewer", reply_to=anchor.id)
            try:
                await self.client.wait_for_bot_message(
                    chat,
                    after_id=before,
                    predicate=lambda text: REVIEWER_CONFIRMED in text,
                )
            except E2ERuntimeError as error:
                raise E2EFailure("not_admin", NOT_ADMIN_DETAIL) from error
        before = await self._last_message_id(self.group_a)
        await self.client.send(self.group_a, "/reviewer")
        await self.client.wait_for_bot_message(
            self.group_a,
            after_id=before,
            predicate=lambda text: REVIEWER_LIST in text,
        )

    async def reject_and_reflag(self) -> None:
        """Reject a correction, then re-flag the same original answer."""
        answer = self._mention_answer
        if answer is None:
            raise E2EFailure("missing_answer", "mention answer was not saved")
        first_prompt = await self._flag_and_propose(
            answer, f"Proposta REJECT-{self.run_id}."
        )
        review = await self._wait_review(first_prompt, f"REJECT-{self.run_id}")
        await self.client.click_button(review, BTN_REJECT)
        await self._wait_dm_needle(REVIEW_REJECTED)

        second_prompt = await self._flag_and_propose(
            answer, f"Esborrany LOCAL-{self.run_id}."
        )
        self._second_review = await self._wait_review(
            second_prompt, f"LOCAL-{self.run_id}"
        )

    async def edit_and_approve_local(self) -> None:
        """Edit the pending correction and approve it for the group only."""
        bot_username = self.settings.telegram_e2e_bot_username
        sentinel_ask = f"@{bot_username} {SENTINEL_QUESTION}"
        review = self._second_review
        if review is None:
            raise E2EFailure("missing_review", "second review was not saved")
        await self.client.click_button(review, BTN_EDIT)
        await self._wait_dm_needle(EDIT_PROMPT)
        prompt = await self._last_dm_message()
        await self.client.reply(prompt, f"El codi E2E local és LOCAL-{self.run_id}.")
        regenerated = await self._wait_review(prompt, f"LOCAL-{self.run_id}")
        await self.client.click_button(regenerated, BTN_APPROVE_GROUP)
        await self._wait_dm_needle(APPROVED)

        await self.ask_until(
            self.group_a,
            sentinel_ask,
            f"LOCAL-{self.run_id}",
        )
        await self.ask_until(
            self.group_b,
            sentinel_ask,
            BASELINE_TOKEN,
            attempts=1,
        )

    async def global_correction_from_b(self) -> None:
        """Propose and globally approve from B; A must keep its local override."""
        bot_username = self.settings.telegram_e2e_bot_username
        sentinel_ask = f"@{bot_username} {SENTINEL_QUESTION}"
        before = await self._last_message_id(self.group_b)
        answer = await self.ask_and_expect(
            self.group_b, sentinel_ask, BASELINE_TOKEN, after_id=before
        )
        prompt = await self._flag_and_propose(
            answer, f"El codi E2E global és GLOBAL-{self.run_id}."
        )
        review = await self._wait_review(prompt, f"GLOBAL-{self.run_id}")
        await self.client.click_button(review, BTN_APPROVE_GLOBAL)
        await self._wait_dm_needle(APPROVED)

        await self.ask_until(
            self.group_b,
            sentinel_ask,
            f"GLOBAL-{self.run_id}",
        )
        await self.ask_until(
            self.group_a,
            sentinel_ask,
            f"LOCAL-{self.run_id}",
            attempts=1,
        )

    async def bot_modes(self) -> None:
        """Walk group A through all four modes and assert what each does.

        This is the mode matrix driven against a real group: `off` neither
        stores nor answers, `silent` stores a mention but stays quiet, `active`
        answers a mention and ignores a bare question, and `proactive` answers a
        confident unaddressed question. Only `addressed` and `proactive` produce
        a visible bot reply; the rest are proven by the silence.
        """
        group = self._require(self.group_a, "group A")
        title = self.settings.telegram_e2e_group_a_title
        bot_username = self.settings.telegram_e2e_bot_username

        # off: a mention gets nothing.
        await self._set_group_mode(group, title, "off")
        before = await self._last_message_id(group)
        await self.client.send(group, f"@{bot_username} {SENTINEL_QUESTION}")
        await self.client.assert_no_bot_message(
            group, after_id=before, seconds=LISTENER_QUIET_SECONDS
        )

        # silent: a mention is stored but still not answered.
        await self._set_group_mode(group, title, "silent")
        before = await self._last_message_id(group)
        await self.client.send(group, f"@{bot_username} {SENTINEL_QUESTION}")
        await self.client.assert_no_bot_message(
            group, after_id=before, seconds=LISTENER_QUIET_SECONDS
        )

        # active: a bare question is not answered, a mention is.
        await self._set_group_mode(group, title, "active")
        before = await self._last_message_id(group)
        await self.client.send(group, SENTINEL_QUESTION)
        await self.client.assert_no_bot_message(
            group, after_id=before, seconds=LISTENER_QUIET_SECONDS
        )
        await self.ask_and_expect(
            group,
            f"@{bot_username} {SENTINEL_QUESTION}",
            BASELINE_TOKEN,
            after_id=await self._last_message_id(group),
        )

        # proactive: a confident unaddressed question earns an answer. Give the
        # listener a moment to classify it and the generator a chance to run.
        await self._set_group_mode(group, title, "proactive")
        answered = await self.ask_and_expect_soft(
            group,
            SENTINEL_QUESTION,
            BASELINE_TOKEN,
            after_id=await self._last_message_id(group),
        )
        if answered is None:
            raise E2EFailure(
                "proactive_not_answered",
                "a confident unaddressed question in a proactive group got no reply",
            )
        # A proactive group must still answer a mention normally.
        await self.ask_and_expect(
            group,
            f"@{bot_username} {SENTINEL_QUESTION}",
            BASELINE_TOKEN,
            after_id=await self._last_message_id(group),
        )
        # Leave the group as it was found: a proactive group would answer
        # unaddressed questions for the rest of the run and for every later one.
        await self._set_group_mode(group, title, DEFAULT_GROUP_MODE)

    async def private_multiscope(self) -> None:
        """Ask privately after group traffic and require a grounded answer.

        By this point the human has written in both served groups, which is
        what the bot observes membership from, so a private question is
        answered from the knowledge those groups share with the club.

        What this step deliberately does not claim: that membership is what
        *gated* the message. The harness account is the admin, so it would be
        answered either way, and production already holds its memberships.
        The gate itself is pinned by the offline integration tests, where a
        sender with no observed membership is refused.
        """
        bot = self._require(self.bot, "bot")
        before = self._dm_last_seen
        await self.client.send(bot, SENTINEL_QUESTION)
        answer = await self.wait_dm(after_id=before, needle=BASELINE_TOKEN)
        if FONTS_LABEL not in (answer.text or ""):
            raise E2EFailure(
                "dm_answer_uncited", "the private answer carried no sources"
            )
        self._dm_last_seen = max(self._dm_last_seen, answer.id)

    async def dedup_dm(self) -> None:
        """Assert a question only one scope can answer is delivered once.

        The collapsing question is seeded into global knowledge and into no
        group, and it is one nobody has ever said in either group. So every
        round lands on the same single source, which is the only situation the
        collapse exists for, and the reader must get one message with no
        heading rather than one per group.

        The sentinel cannot play this part: every run says it out loud in both
        groups, the listener indexes it as message evidence, and each group's
        round then cites its own. That is two different answers by design, not
        a failure to collapse, and the first run of this step is what proved
        the difference.
        """
        bot = self._require(self.bot, "bot")
        await self.worker.seed_collapse_question(self.run_id)
        await asyncio.sleep(PROJECTION_SETTLE_SECONDS)

        before = self._dm_last_seen
        await self.client.send(bot, COLLAPSE_QUESTION)
        first = await self.wait_dm(
            after_id=before, needle=COLLAPSE_TOKEN.format(run_id=self.run_id)
        )
        text = first.text or ""
        if GLOBAL_LABEL in text or GROUP_LABEL_MARK in text:
            raise E2EFailure(
                "dedup_labelled",
                "one shared source was delivered as several labelled answers",
            )
        await self.client.assert_no_bot_message(
            bot, after_id=first.id, seconds=DM_DEDUP_QUIET_SECONDS
        )
        self._dm_last_seen = max(self._dm_last_seen, first.id)

    async def _set_group_mode(self, group: object, title: str, mode: str) -> None:
        """Set a served group's bot mode through the internal route.

        Args:
            group: The resolved group entity.
            title: The exact `[E2E]` title the group is registered with.
            mode: One of `off`, `silent`, `active`, `proactive`.

        Raises:
            E2EFailure: If the Worker does not accept the mode.
        """
        chat_id = str(utils.get_peer_id(group))
        await self.worker.register_group(
            chat_id, title, self._space_id(group), bot_mode=mode
        )

    def _space_id(self, group: object) -> str:
        """Return the fixed logical space id for a served group.

        Args:
            group: The resolved group entity.

        Returns:
            `SPACE_A` or `SPACE_B` based on the entity.

        Raises:
            E2EFailure: If the group is neither A nor B.
        """
        if group is self.group_a:
            return SPACE_A
        if group is self.group_b:
            return SPACE_B
        raise E2EFailure(
            "unknown_group", "group is not one of the dedicated E2E groups"
        )

    async def daily_report(self) -> None:
        """Force the daily report and require its Telegram delivery."""
        before = self._dm_last_seen
        await self.worker.run_daily_report()
        report = await self.wait_dm(
            after_id=before,
            needle=REPORT_TITLE,
        )
        for section in REPORT_SECTIONS:
            if section not in report.text:
                raise E2EFailure(
                    "report_section_missing", f"daily report lacks {section!r}"
                )
        self._dm_last_seen = max(self._dm_last_seen, report.id)

    # --------------------------------------------------------------- cleanup

    async def cleanup(self) -> None:
        """Restore modes, remove reviewers, and revert both sentinel items.

        The mode reset is the part that must not be skipped: a run that leaves
        a dedicated group in ``proactive`` or ``off`` would change the behaviour
        every later run and every real member sees.

        Raises:
            E2EFailure: If the mode reset, reviewer cleanup, or a sentinel
                revert fails (stale state would change future runs).
        """
        problems: list[str] = []
        await self._reset_group_modes(problems)
        for chat, name in (
            (self.group_a, "group A"),
            (self.group_b, "group B"),
        ):
            if chat is None:
                continue  # preflight never resolved it; nothing to clean up
            try:
                await self.client.send(chat, "/reviewer off")
                before = await self._last_message_id(chat)
                await self.client.wait_for_bot_message(
                    chat,
                    after_id=before,
                    predicate=lambda text: (
                        REVIEWER_REMOVED in text or REVIEWER_ABSENT in text
                    ),
                )
            except Exception as error:
                problems.append(f"{name} reviewer cleanup failed: {error}")
        try:
            await self.worker.reset_collapse_question()
            await self.worker.reset_item(
                qa_item_id_for(SENTINEL_QUESTION, f"space:{SPACE_A}")
            )
            await self.worker.reset_item(
                qa_item_id_for(SENTINEL_QUESTION, SCOPE_GLOBAL)
            )
        except Exception as error:
            problems.append(f"sentinel revert cleanup failed: {error}")
        if problems:
            raise E2EFailure("cleanup_failed", "; ".join(problems))

    async def _reset_group_modes(self, problems: list[str]) -> None:
        """Return both served groups to ``active`` after a mode-matrix run.

        Args:
            problems: Collected cleanup failures, appended to in place.
        """
        for group, title, name in (
            (self.group_a, self.settings.telegram_e2e_group_a_title, "group A"),
            (self.group_b, self.settings.telegram_e2e_group_b_title, "group B"),
        ):
            if group is None:
                continue  # preflight never resolved it; nothing to restore
            try:
                await self._set_group_mode(group, title, DEFAULT_GROUP_MODE)
            except Exception as error:
                problems.append(f"{name} mode reset failed: {error}")

    # --------------------------------------------------------------- helpers

    async def _flag_and_propose(self, answer: Message, proposal: str) -> Message:
        """Flag a bot answer and reply to the private proposal prompt.

        Args:
            answer: The bot answer message carrying the flag button.
            proposal: The correction proposal text.

        Returns:
            The force-reply prompt message (for later relative waits).
        """
        await self.client.click_button(answer, BTN_FLAG)
        prompt = await self.wait_dm(needle=PROPOSAL_PROMPT)
        await self.client.reply(prompt, proposal)
        ack = await self.wait_dm(after_id=prompt.id, needle=PROPOSAL_ACK)
        self._dm_last_seen = max(self._dm_last_seen, ack.id)
        return prompt

    async def _wait_review(self, after: Message, token: str) -> Message:
        """Wait for the reviewer DM containing the proposal token.

        Args:
            after: The message that started this correction.
            token: The proposal token the review must echo.

        Returns:
            The review message (which carries the decision buttons).
        """
        return await self.client.wait_for_bot_message(
            self.bot,
            after_id=after.id,
            predicate=lambda text: token in text and REVIEW_HEADER in text,
        )

    async def _wait_dm_needle(self, needle: str) -> Message:
        """Wait for any new bot DM containing a needle.

        Args:
            needle: The substring to look for.

        Returns:
            The matching bot DM message.
        """
        message = await self.wait_dm(after_id=self._dm_last_seen, needle=needle)
        self._dm_last_seen = max(self._dm_last_seen, message.id)
        return message

    async def _last_dm_message(self) -> Message:
        """Return the newest message currently visible in the bot DM.

        Returns:
            The newest DM message.

        Raises:
            E2EFailure: If the DM has no messages at all.
        """
        bot = self._require(self.bot, "bot")
        fetched = await self.client.raw.get_messages(bot, limit=1)
        first = cast(Sequence[Message | None], fetched)[0]
        if first is None:
            raise E2EFailure("empty_dm", "no messages in the bot DM")
        return first


STEPS: tuple[tuple[str, str], ...] = (
    ("preflight", "preflight"),
    ("mention, /ask, reply and DM", "addressing"),
    ("abstention", "abstention"),
    ("listener + reply pairing", "listener"),
    ("private answer across served groups", "private_multiscope"),
    ("one shared answer, one delivery", "dedup_dm"),
    ("local reviewers in A and B", "reviewer_setup"),
    ("reject and re-flag", "reject_and_reflag"),
    ("edit + local approval", "edit_and_approve_local"),
    ("global correction from B", "global_correction_from_b"),
    ("bot modes: off/silent/active/proactive", "bot_modes"),
    ("daily report", "daily_report"),
)


async def main() -> None:
    """Run the whole scenario, print progress, clean up, exit non-zero on fail."""
    settings = TelegramE2ESettings()
    try:
        settings.validate_strict()
    except E2EConfigError as error:
        print(f"E2E configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    print(f"Bot @{settings.telegram_e2e_bot_username}, live run authorized")
    client = TelegramE2EClient(settings)
    scenario = Scenario(settings, client)
    failure: BaseException | None = None
    try:
        await client.connect()
        for index, (label, method) in enumerate(STEPS, start=1):
            print(f"[{index:02d}/{len(STEPS)}] {label:<40}", end="", flush=True)
            started = time.perf_counter()
            try:
                await getattr(scenario, method)()
            except Exception as error:
                print(f" FAIL ({error})")
                if failure is None:
                    wrapped = error if isinstance(error, E2EFailure) else None
                    failure = wrapped or E2EFailure(str(error))
                break
            print(f" ok ({time.perf_counter() - started:.1f}s)")
    finally:
        print("[cleanup] removing reviewers and reverting sentinels", flush=True)
        try:
            await scenario.cleanup()
            print("[cleanup] ok")
        except Exception as cleanup_error:
            print(f"[cleanup] FAILED: {cleanup_error}")
            failure = failure or cleanup_error
        await scenario.close()
        await client.close()
    if failure is not None:
        print("FAIL: real Telegram E2E")
        raise SystemExit(1)
    print("PASS: real Telegram E2E")


if __name__ == "__main__":
    asyncio.run(main())
