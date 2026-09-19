# SPDX-License-Identifier: MIT
"""Cloudflare Worker entrypoint for the knowledge bot."""

from workers import asgi, env

from knowledge_bot.adapters.inbound.fastapi_routes import create_app
from knowledge_bot.infrastructure.composition import build_context

app = create_app(build_context(env))

Default = asgi.entrypoint(app)
