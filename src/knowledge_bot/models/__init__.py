# SPDX-License-Identifier: MIT
"""Pydantic DTOs used at external boundaries.

``common``, ``messages``, ``questions``, ``feedback``, ``operations``, and
``seed`` hold the models shared across channels. Telegram payload models are not
here: they belong to that connector, under ``adapters/telegram/``.
"""
