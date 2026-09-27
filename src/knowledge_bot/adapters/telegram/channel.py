# SPDX-License-Identifier: MIT
"""Telegram-specific dependencies bundled for the connector.

``AppContext`` used to expose a bare ``identity`` and ``transport`` field typed
as Telegram types, which made the shared context claim to be Telegram. The
shared application services never touch either: they are only ever used by the
adapter, so they belong here where the connector is visible as a whole.
"""

from dataclasses import dataclass

from knowledge_bot.adapters.telegram.client import TelegramClient
from knowledge_bot.adapters.telegram.identity import TelegramIdentity


@dataclass(frozen=True, slots=True)
class TelegramChannel:
    """The identity and client the Telegram adapter needs to serve a request."""

    identity: TelegramIdentity
    client: TelegramClient
