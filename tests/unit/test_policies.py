# SPDX-License-Identifier: MIT
"""Tests for the authority and evidence policies."""

from knowledge_bot.domain.enums import QAOrigin, SourceType
from knowledge_bot.domain.policies import (
    Authority,
    highest_authority,
    is_decisive_alone,
    origin_authority,
    source_authority,
    telegram_authority,
    web_seed_authority,
)


def test_in_review_is_not_decisive_alone() -> None:
    """A single in-review web entry cannot establish a fact."""
    assert not is_decisive_alone(Authority.WEB_IN_REVIEW)
    assert is_decisive_alone(Authority.WEB_PUBLISHED)


def test_admin_approval_dominates() -> None:
    """Admin-approved knowledge outranks every other source."""
    assert (
        highest_authority([Authority.TELEGRAM_USER, Authority.ADMIN_APPROVED])
        is Authority.ADMIN_APPROVED
    )


def test_highest_authority_of_empty_list_is_none() -> None:
    """There is no authority without evidence."""
    assert highest_authority([]) is None


def test_telegram_authority_depends_on_admin_flag() -> None:
    """Admin messages outrank regular member messages."""
    assert telegram_authority(is_admin=True) is Authority.TELEGRAM_ADMIN
    assert telegram_authority(is_admin=False) is Authority.TELEGRAM_USER


def test_web_seed_authority_reflects_review_status() -> None:
    """In-review web entries rank below published ones."""
    assert web_seed_authority(in_review=True) is Authority.WEB_IN_REVIEW
    assert web_seed_authority(in_review=False) is Authority.WEB_PUBLISHED


def test_source_and_origin_authority_mapping() -> None:
    """Baseline authority matches the spec table."""
    assert source_authority(SourceType.WHATSAPP_IMPORT) is Authority.WHATSAPP_IMPORT
    assert source_authority(SourceType.TELEGRAM) is Authority.TELEGRAM_USER
    assert origin_authority(QAOrigin.ADMIN_APPROVED) is Authority.ADMIN_APPROVED
    assert origin_authority(QAOrigin.AUTO_GENERATED) is Authority.AUTO_GENERATED
