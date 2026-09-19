# SPDX-License-Identifier: MIT
"""Cloudflare Worker entrypoint for the knowledge bot."""

from workers import asgi

from knowledge_bot.adapters.inbound.fastapi_routes import app

Default = asgi.entrypoint(app)
