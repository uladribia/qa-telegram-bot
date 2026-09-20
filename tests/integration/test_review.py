# SPDX-License-Identifier: MIT
"""Integration tests for the human knowledge review report."""

from datetime import UTC, datetime

from knowledge_bot.application.review import ReviewService, render_review_report
from knowledge_bot.ports.review import ReviewItem
from tests.fakes.ai import FakeReviewSource

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)


def _item(
    key: str,
    scope: str,
    answer: str,
    *,
    origin: str = "web_seed",
    status: str = "active",
    superseded_origin: str | None = None,
    question: str = "Què?",
) -> ReviewItem:
    return ReviewItem(
        canonical_key=key,
        question=question,
        scope=scope,
        answer=answer,
        origin=origin,
        created_at=NOW,
        status=status,
        superseded_origin=superseded_origin,
    )


async def test_divergent_variant_is_a_finding() -> None:
    """A group variant differing from the global answer is reported."""
    source = FakeReviewSource(
        [
            _item("k1", "global", "Resposta global"),
            _item("k1", "-100", "Resposta del grup"),
        ]
    )
    entries = await ReviewService(source).review()
    assert len(entries) == 1
    assert entries[0].divergent is True
    assert entries[0].global_answer == "Resposta global"
    text = render_review_report(entries)
    assert "Què?" in text
    assert "Resposta del grup" in text
    assert "variant de grup diferent de la global" in text


async def test_matching_variant_is_not_a_finding() -> None:
    """A group variant equal to the global answer is not reported."""
    source = FakeReviewSource(
        [
            _item("k1", "global", "Igual"),
            _item("k1", "-100", "Igual"),
        ]
    )
    assert await ReviewService(source).review() == []


async def test_renewal_over_a_correction_is_flagged() -> None:
    """A web renewal that superseded an approved correction is flagged."""
    source = FakeReviewSource(
        [
            _item(
                "k1",
                "global",
                "Web renovat.",
                superseded_origin="admin_approved",
            )
        ]
    )
    entries = await ReviewService(source).review()
    assert entries[0].renewal_overrode_correction is True
    text = render_review_report(entries)
    assert "renovació web sobre una correcció" in text


async def test_under_review_and_corrections_are_flagged() -> None:
    """Corrections and in-review entries appear with their flags."""
    source = FakeReviewSource(
        [
            _item("k1", "global", "Corregit", origin="admin_approved"),
            _item("k2", "global", "Pendent", status="under_review"),
        ]
    )
    entries = await ReviewService(source).review()
    assert len(entries) == 2
    text = render_review_report(entries)
    assert "correcció aprovada" in text
    assert "sota revisió" in text


async def test_empty_report_when_nothing_diverges() -> None:
    """With no findings the report says so."""
    text = render_review_report([])
    assert "Cap trobada per revisar" in text
