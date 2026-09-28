# SPDX-License-Identifier: MIT

"""External black-box Telegram E2E for the deployed knowledge bot.

This package is deliberately outside `src/knowledge_bot/`: it drives the
deployed Worker through real Telegram with Telethon, using the existing human
admin account. It is never shipped, never imported by production code, and owns
Telethon as an optional `e2e` dependency group only.
"""
