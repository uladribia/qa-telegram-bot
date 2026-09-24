# SPDX-License-Identifier: MIT
"""Tests for generic source and principal identity helpers."""

import pytest

from knowledge_bot.domain.identity import principal_id, source_instance_id


def test_source_instance_id_is_deterministic_and_connector_owned() -> None:
    """The helper hashes caller-provided connector identity parts."""
    first = source_instance_id("matrix_room", "room-1", "snapshot-1")
    second = source_instance_id("matrix_room", "room-1", "snapshot-1")
    different = source_instance_id("matrix_room", "room-2", "snapshot-1")
    assert first == second
    assert first.startswith("src:matrix_room:")
    assert first != different


def test_source_instance_id_rejects_empty_identity() -> None:
    """A source kind and every identity part are required."""
    with pytest.raises(ValueError):
        source_instance_id("", "room-1")


def test_principal_id_preserves_opaque_external_identity() -> None:
    """Principal ids are channel-prefixed without interpreting the suffix."""
    assert principal_id("matrix", "opaque-user") == "matrix:opaque-user"
