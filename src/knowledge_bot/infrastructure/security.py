# SPDX-License-Identifier: MIT
"""Secret comparison helpers."""

import hmac


def secrets_match(provided: str | None, expected: str) -> bool:
    """Compare two secrets in constant time.

    Args:
        provided: The value supplied by the caller.
        expected: The configured secret.

    Returns:
        ``True`` only when both are non-empty and equal.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)
