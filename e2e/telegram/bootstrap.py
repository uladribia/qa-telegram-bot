# SPDX-License-Identifier: MIT

"""One-time interactive login for the existing human Telegram account.

Run via `make telegram-e2e-login`. Telethon prompts for the phone number,
login code and 2FA password on the terminal; the session is persisted under
`.e2e/` and only the display name, username and numeric user id are printed.
"""

import asyncio

from telethon import TelegramClient, utils

from e2e.telegram.config import TelegramE2ESettings


async def main() -> None:
    """Log the existing human account in and print only safe identity facts.

    Raises:
        SystemExit: If the configuration is invalid.
    """
    settings = TelegramE2ESettings()
    settings.validate_strict()

    session_path = settings.session_path
    session_path.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(
        str(session_path),
        settings.telegram_e2e_api_id,
        settings.telegram_e2e_api_hash,
    )
    await client.start()  # ty: ignore[invalid-await]
    me = await client.get_me()
    username = getattr(me, "username", None) or "(no username)"
    print("E2E login ok:")
    print(f"  display name: {getattr(me, 'first_name', '')}".rstrip())
    print(f"  username:     {username}")
    print(f"  user id:      {utils.get_peer_id(me)}")
    print(f"  session file: {session_path}(.session)")
    await client.disconnect()


def _entry() -> None:
    """Run the login coroutine."""
    asyncio.run(main())


if __name__ == "__main__":
    _entry()
