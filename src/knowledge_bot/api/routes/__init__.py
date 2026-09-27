# SPDX-License-Identifier: MIT
"""Canonical, channel-independent HTTP routes.

Nothing under ``api/routes`` except the internal operator endpoints knows which
channel a request came from. A question asked over HTTP and a question asked in
a Telegram group reach the same application services.
"""
