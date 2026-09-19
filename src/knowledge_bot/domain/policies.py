# SPDX-License-Identifier: MIT
"""Domain policies for source authority (spec §10).

Authority values are a policy of the prototype, not probabilities.
"""

from enum import IntEnum

from knowledge_bot.domain.enums import QAOrigin, SourceType


class Authority(IntEnum):
    """Authority ranking of evidence; a higher value wins."""

    WEB_IN_REVIEW = 30
    TELEGRAM_USER = 40
    WHATSAPP_IMPORT = 50
    AUTO_GENERATED = 60
    WEB_PUBLISHED = 90
    TELEGRAM_ADMIN = 95
    ADMIN_APPROVED = 100


def telegram_authority(*, is_admin: bool) -> Authority:
    """Return the authority of a Telegram message.

    Args:
        is_admin: Whether the sender is the configured administrator.

    Returns:
        The matching authority level.
    """
    return Authority.TELEGRAM_ADMIN if is_admin else Authority.TELEGRAM_USER


def web_seed_authority(*, in_review: bool) -> Authority:
    """Return the authority of a web Q&A snapshot entry.

    Args:
        in_review: Whether the entry is still marked as in review.

    Returns:
        The matching authority level.
    """
    return Authority.WEB_IN_REVIEW if in_review else Authority.WEB_PUBLISHED


def source_authority(source_type: SourceType) -> Authority:
    """Return the baseline authority for a source type.

    Args:
        source_type: The source being ranked.

    Returns:
        The baseline authority level for that source type.
    """
    match source_type:
        case SourceType.WEB_SEED:
            return Authority.WEB_PUBLISHED
        case SourceType.WHATSAPP_IMPORT:
            return Authority.WHATSAPP_IMPORT
        case SourceType.TELEGRAM:
            return Authority.TELEGRAM_USER
        case SourceType.ADMIN:
            return Authority.ADMIN_APPROVED


def origin_authority(origin: QAOrigin) -> Authority:
    """Return the authority assigned to a new Q&A version origin.

    Args:
        origin: How the version was produced.

    Returns:
        The authority level for that origin.
    """
    match origin:
        case QAOrigin.ADMIN_APPROVED:
            return Authority.ADMIN_APPROVED
        case QAOrigin.AUTO_GENERATED:
            return Authority.AUTO_GENERATED
        case QAOrigin.WEB_SEED:
            return Authority.WEB_PUBLISHED


def highest_authority(authorities: list[Authority]) -> Authority | None:
    """Return the strongest authority in a list, if any.

    Args:
        authorities: Authority levels to compare.

    Returns:
        The highest authority, or ``None`` when the list is empty.
    """
    return max(authorities) if authorities else None


def is_decisive_alone(authority: Authority) -> bool:
    """Whether a single piece of evidence may establish a fact.

    Args:
        authority: The evidence authority.

    Returns:
        ``False`` for low-authority evidence such as in-review web entries.
    """
    return authority > Authority.WEB_IN_REVIEW
