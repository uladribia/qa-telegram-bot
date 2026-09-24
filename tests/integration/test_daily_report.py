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
    report = transport.messages[0][1]
    assert "Resum diari" in report
    assert "Preguntes adreçades" in report
    assert "Preguntes de fons" in report
    assert "Ingesta de fons" in report
    assert "Correccions" in report
    assert "Divergència de seeds" in report
    assert "IA:" in report


def test_manual_daily_report_can_force_a_due_run() -> None:
    """Operators can explicitly request a report outside the daily window."""
    context, transport = build_test_context()

    assert asyncio.run(context.daily_report.run(force=True)) is True
    assert asyncio.run(context.daily_report.run(force=True)) is True
    assert len(transport.messages) == 2
