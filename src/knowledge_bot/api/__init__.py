# SPDX-License-Identifier: MIT
"""Canonical API application.

``api`` owns the FastAPI application itself. It is not a channel adapter: it
composes the canonical routes, registers the enabled connectors, and does
nothing else.
"""
