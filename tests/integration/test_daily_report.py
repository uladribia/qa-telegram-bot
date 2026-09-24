# SPDX-License-Identifier: MIT
"""Integration tests for the deterministic daily report job."""

import asyncio

from tests.fakes.context import build_test_context


def test_daily_report_runs_once_per_window_without_ai() -> None:
    """The same job can be invoked manually or by the scheduler."""
    context, transport = build_test_context()

    first = asyncio.run(context.daily_report.run())
    second = asyncio.run(context.daily_report.run())

    assert first is True
    assert second is False
    assert len(transport.messages) == 1
    assert "Resum diari" in transport.messages[0][1]


def test_manual_daily_report_can_force_a_due_run() -> None:
    """Operators can explicitly request a report outside the daily window."""
    context, transport = build_test_context()

    assert asyncio.run(context.daily_report.run(force=True)) is True
    assert asyncio.run(context.daily_report.run(force=True)) is True
    assert len(transport.messages) == 2
