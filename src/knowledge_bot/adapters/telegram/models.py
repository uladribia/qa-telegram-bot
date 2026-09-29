# SPDX-License-Identifier: MIT
"""Telegram boundary models.

A minimal, typed view of the Telegram Bot API objects the adapter consumes.
Unknown fields are ignored so the API can evolve without breaking us. These are
connector payloads: nothing outside the Telegram adapter imports them.
"""

from pydantic import BaseModel, ConfigDict, Field

from knowledge_bot.domain.enums import BotMode


class RegisterGroupRequest(BaseModel):
    """Telegram group registration request.

    ``title`` and ``bot_mode`` are optional on purpose: omitting one keeps
    whatever is already registered, so a mode can be changed without renaming
    the group and the other way round.
    """

    chat_id: str = Field(default="", min_length=1)
    title: str | None = None
    space_id: str | None = None
    bot_mode: BotMode | None = None


class TelegramChat(BaseModel):
    """A Telegram chat."""

    model_config = ConfigDict(extra="ignore")

    id: int
    type: str
    username: str | None = None


class TelegramUser(BaseModel):
    """A Telegram user."""

    model_config = ConfigDict(extra="ignore")

    id: int
    is_bot: bool = False
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None


class TelegramPhotoSize(BaseModel):
    """One size of a Telegram photo."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str
    width: int
    height: int
    file_size: int | None = None


class TelegramDocument(BaseModel):
    """A Telegram document attachment."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


class TelegramMessage(BaseModel):
    """The subset of a Telegram message the adapter needs."""

    model_config = ConfigDict(extra="ignore")

    message_id: int
    date: int
    chat: TelegramChat
    from_user: TelegramUser | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None
    photo: list[TelegramPhotoSize] = Field(default_factory=list)
    document: TelegramDocument | None = None
    voice: TelegramDocument | None = None
    audio: TelegramDocument | None = None
    video: TelegramDocument | None = None
    reply_to_message: "TelegramMessage | None" = None
    #: Join service messages. Telegram sends one update listing everyone who
    #: was added, including the bot itself when it joins.
    new_chat_members: list[TelegramUser] = Field(default_factory=list)
    #: Leave service messages, including a kick: Telegram reports both the same
    #: way, and the bot cannot tell them apart without admin rights.
    left_chat_member: TelegramUser | None = None


class TelegramCallbackQuery(BaseModel):
    """A Telegram inline-button callback."""

    model_config = ConfigDict(extra="ignore")

    id: str
    from_user: TelegramUser = Field(alias="from")
    data: str | None = None
    message: TelegramMessage | None = None


class TelegramUpdate(BaseModel):
    """A Telegram update envelope."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None
    edited_message: TelegramMessage | None = None


class NormalizedCallback(BaseModel):
    """A callback query reduced to what the feedback flow needs.

    ``sender_chat_id`` is a raw Telegram user id used only to open a private
    chat with the reporter; it is a routing key, never displayed or logged.
    ``sender_name`` is the display name used as the citation author when the
    proposal is approved.
    """

    callback_id: str
    data: str | None = None
    sender_id: str | None = None
    sender_chat_id: str | None = None
    sender_name: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
