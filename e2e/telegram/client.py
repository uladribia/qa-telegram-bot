# SPDX-License-Identifier: MIT

"""Small Telethon helper for the real Telegram E2E run.

Only the operations the scenario needs live here: connect, resolve the bot and
the dedicated groups by exact title, send, poll for new bot messages, press the
real inline buttons, and assert silence. The harness never inspects callback
payloads; it presses the buttons a human presses.

Telethon ships loose type stubs (its own client methods are annotated as
`Any`), so the narrow casts below are the harness's contract with the
type-checker, not business logic.
"""

import asyncio
from collections.abc import Callable, Sequence
from typing import cast

from telethon import TelegramClient, utils
from telethon.tl.custom import message as message_module

from e2e.telegram.config import POLL_INTERVAL_SECONDS, TelegramE2ESettings

Message = message_module.Message
Predicate = Callable[[str], bool]


class E2ERuntimeError(RuntimeError):
    """Raised when a live Telegram interaction did not behave as expected.

    Args:
        code: A short stable label for the failure, safe to print.
        detail: Optional longer human-readable context appended after the code.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        """Store the code and optional detail."""
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class TelegramE2EClient:
    """Thin wrapper over Telethon for one sequential E2E scenario.

    Args:
        settings: Validated E2E settings.

    Attributes:
        settings: The validated settings.
    """

    def __init__(self, settings: TelegramE2ESettings) -> None:
        """Create the client without connecting yet."""
        self.settings = settings
        self._client = TelegramClient(
            str(settings.session_path),
            settings.telegram_e2e_api_id,
            settings.telegram_e2e_api_hash,
        )
        self._bot_id: int | None = None
        self._groups: dict[str, object] = {}

    @property
    def raw(self) -> TelegramClient:
        """Return the underlying Telethon client (bootstrap login needs it)."""
        return self._client

    async def __aenter__(self) -> "TelegramE2EClient":
        """Connect on context entry."""
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Disconnect on context exit."""
        await self.close()

    async def connect(self) -> None:
        """Connect and raise if the session is not authorized."""
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise E2ERuntimeError("unauthorized_session")

    async def close(self) -> None:
        """Disconnect the client."""
        await self._client.disconnect()

    async def get_me(self) -> object:
        """Return the logged-in human user object."""
        return cast(object, await self._client.get_me())

    async def resolve_bot(self, username: str) -> object:
        """Resolve the bot entity and cache its numeric id.

        Args:
            username: The bot's @username, without the @.

        Returns:
            The resolved bot user entity.

        Raises:
            E2ERuntimeError: If the entity is not a bot.
        """
        entity = cast(object, await self._client.get_entity(username))
        bot_id = utils.get_peer_id(entity)
        if not getattr(entity, "bot", False):
            detail = f"resolved entity @{username} is not a bot"
            raise E2ERuntimeError("not_a_bot", detail)
        self._bot_id = bot_id
        return entity

    @property
    def bot_id(self) -> int:
        """Return the cached Bot-API-compatible signed bot user id."""
        if self._bot_id is None:
            raise E2ERuntimeError("bot_not_resolved")
        return self._bot_id

    async def resolve_group_by_exact_title(self, title: str) -> object:
        """Resolve a dedicated E2E group from dialogs by exact title.

        Args:
            title: The exact configured group title (must start with `[E2E]`).

        Returns:
            The resolved group entity.

        Raises:
            E2ERuntimeError: If no group dialog matches the exact title.
        """
        async for dialog in self._client.iter_dialogs():
            if getattr(dialog, "is_group", False) and dialog.name == title:
                self._groups[title] = dialog.entity
                return cast(object, dialog.entity)
        detail = f"no group dialog with exact title {title!r}"
        raise E2ERuntimeError("group_not_found", detail)

    async def group_a(self) -> object:
        """Resolve dedicated E2E group A."""
        return await self.resolve_group_by_exact_title(
            self.settings.telegram_e2e_group_a_title
        )

    async def group_b(self) -> object:
        """Resolve dedicated E2E group B."""
        return await self.resolve_group_by_exact_title(
            self.settings.telegram_e2e_group_b_title
        )

    async def send(
        self,
        chat: object,
        text: str,
        *,
        reply_to: int | None = None,
    ) -> Message:
        """Send a message to a chat (group or bot DM).

        Args:
            chat: The resolved chat entity.
            text: The message text.
            reply_to: Optional message id to reply to.

        Returns:
            The sent Telethon message.
        """
        sent = await self._client.send_message(
            chat,  # ty: ignore[invalid-argument-type]
            text,
            reply_to=reply_to,  # ty: ignore[invalid-argument-type]
        )
        return cast(Message, sent)

    async def reply(self, message: Message, text: str) -> Message:
        """Explicitly reply to a chat message (human or bot) with text.

        Args:
            message: The message to answer.
            text: The reply text.

        Returns:
            The sent Telethon message.
        """
        sent = await self._client.send_message(
            message.peer_id, text, reply_to=message.id
        )
        return cast(Message, sent)

    async def click_button(self, message: Message, label: str) -> None:
        """Press the inline button whose visible label matches exactly.

        Args:
            message: The bot message carrying the buttons.
            label: The exact visible label to press.

        Raises:
            E2ERuntimeError: When no button with that label exists (yet).
        """
        deadline = asyncio.get_running_loop().time() + self.settings.timeout_seconds
        not_pressed = f"button {label!r} could not be pressed"
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            fresh = await self._refresh(message)
            rows = fresh.buttons or []
            for row in rows:
                for button in row:
                    if getattr(button, "text", None) == label:
                        try:
                            await fresh.click(text=label)
                        except Exception as error:
                            if asyncio.get_running_loop().time() >= deadline:
                                raise E2ERuntimeError(
                                    "button_failed", not_pressed
                                ) from error
                        else:
                            return
                else:
                    continue
                return
            if asyncio.get_running_loop().time() >= deadline:
                missing = (
                    f"button {label!r} not found within "
                    f"{self.settings.timeout_seconds} seconds"
                )
                raise E2ERuntimeError("button_missing", missing)

    async def wait_for_bot_message(
        self,
        chat: object,
        *,
        after_id: int,
        predicate: Predicate | None = None,
        timeout: int | None = None,
    ) -> Message:
        """Poll a chat until a new bot message that matches appears.

        Args:
            chat: The resolved chat entity.
            after_id: Only messages strictly newer than this id count.
            predicate: Optional visible-text matcher.
            timeout: Maximum seconds to wait; defaults to the configured one.

        Returns:
            The first matching bot message.

        Raises:
            E2ERuntimeError: If no matching message appears in time.
        """
        limit = timeout if timeout is not None else self.settings.timeout_seconds
        deadline = asyncio.get_running_loop().time() + limit
        seen: set[int] = set()
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            fetched = await self._client.get_messages(chat, limit=25)
            for candidate in cast(Sequence[Message], fetched):
                if candidate.id in seen or candidate.id <= after_id:
                    continue
                seen.add(candidate.id)
                if candidate.sender_id != self.bot_id:
                    continue
                if predicate is not None and not predicate(candidate.text or ""):
                    continue
                return candidate
        detail = f"no matching bot message in {limit}s (after_id={after_id})"
        raise E2ERuntimeError("bot_message_timeout", detail)

    async def assert_no_bot_message(
        self,
        chat: object,
        *,
        after_id: int,
        seconds: int,
    ) -> None:
        """Poll a chat for `seconds` and fail if any new bot message appears.

        Args:
            chat: The resolved chat entity.
            after_id: Only messages strictly newer than this id count.
            seconds: The quiet window length.

        Raises:
            E2ERuntimeError: If a new bot message arrives in the window.
        """
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, 1.0))
            fetched = await self._client.get_messages(chat, limit=10)
            for candidate in cast(Sequence[Message], fetched):
                if candidate.id > after_id and candidate.sender_id == self.bot_id:
                    detail = f"bot message {candidate.id} arrived during quiet window"
                    raise E2ERuntimeError("unexpected_bot_message", detail)

    async def _refresh(self, stale: Message) -> Message:
        """Re-fetch a message so freshly edited buttons become visible.

        Args:
            stale: A previously seen message object.

        Returns:
            The newest version of that message.
        """
        refetched = await self._client.get_messages(stale.peer_id, ids=[stale.id])
        first = cast(Sequence[Message | None], refetched)[0]
        return first if first is not None else stale
