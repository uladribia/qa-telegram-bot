# SPDX-License-Identifier: MIT
"""Tests for how sources are rendered in an answer."""

from knowledge_bot.application.answer_question import render_source_line
from knowledge_bot.application.retrieval import Evidence


def test_web_source_shows_url_and_date() -> None:
    """A web source cites its URL and date, not an author."""
    source = Evidence(
        source_id="qav-1",
        label="Q&A",
        text="Els dimarts.",
        authority=90,
        similarity=0.9,
        question="Quan entrenen?",
        url="https://example.com/#qa-x",
        date="2026-09-19",
    )
    line = render_source_line(source)
    assert "https://example.com/#qa-x" in line
    assert "2026-09-19" in line
    assert "Quan entrenen?" in line


def test_group_source_shows_author_and_date() -> None:
    """A group source cites the author and date, not a URL."""
    source = Evidence(
        source_id="m1",
        label="Grup",
        text="els dimarts",
        authority=40,
        similarity=0.6,
        author="Ada",
        date="2026-09-18",
    )
    line = render_source_line(source)
    assert "Ada" in line
    assert "2026-09-18" in line
    assert "http" not in line


def test_source_without_details_still_renders() -> None:
    """A source with no URL, author, or date still renders a bullet."""
    source = Evidence(
        source_id="m2", label="Grup", text="x", authority=40, similarity=0.5
    )
    assert render_source_line(source) == "\u2022 Grup"
