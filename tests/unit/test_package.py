# SPDX-License-Identifier: MIT
"""Smoke tests for the package import surface."""

import knowledge_bot


def test_package_is_importable() -> None:
    """The top-level package exposes a docstring."""
    assert knowledge_bot.__doc__
