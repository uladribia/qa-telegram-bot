# SPDX-License-Identifier: MIT
"""Migration tests: apply the D1 schema to SQLite and check key invariants."""

import sqlite3
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

EXPECTED_TABLES = {
    "sources",
    "spaces",
    "channel_bindings",
    "conversations",
    "messages",
    "attachments",
    "qa_items",
    "qa_versions",
    "qa_evidence",
    "bot_answers",
    "feedback",
    "recap_state",
    "reviewers",
    "reviewer_events",
    "report_state",
    "search_projection",
    "delivery_receipts",
    "telegram_interactions",
    "daily_report_state",
}


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _apply(connection: sqlite3.Connection) -> None:
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        connection.executescript(path.read_text(encoding="utf-8"))


def test_migrations_create_expected_tables() -> None:
    """Every table from the data model is created."""
    connection = _connect()
    _apply(connection)
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert {row[0] for row in rows} >= EXPECTED_TABLES


def test_expected_indexes_exist() -> None:
    """Migrations create the indexes used by the core query paths."""
    connection = _connect()
    _apply(connection)
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    assert {"idx_messages_conversation", "idx_qa_versions_qa"} <= {
        row[0] for row in rows
    }


def _seed_source_and_conversation(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO sources"
        " (id, source_type, external_ref, title, canonical_url, authority,"
        " is_mutable, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("s1", "telegram", None, None, None, 95, 0, "2026-01-01"),
    )
    connection.execute(
        "INSERT INTO conversations"
        " (id, source_id, external_id, title, created_at) VALUES (?, ?, ?, ?, ?)",
        ("c1", "s1", None, None, "2026-01-01"),
    )


def test_knowledge_identity_columns_exist() -> None:
    """Semantic scope and source anchors survive the v2 schema."""
    connection = _connect()
    _apply(connection)
    qa_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(qa_items)").fetchall()
    }
    version_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(qa_versions)").fetchall()
    }
    source_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(sources)").fetchall()
    }
    assert "scope" not in qa_columns | source_columns
    assert "scope_key" in qa_columns & source_columns
    assert "source_anchor" in version_columns


def test_channel_binding_requires_a_space() -> None:
    """External channel bindings cannot point at an unknown space."""
    connection = _connect()
    _apply(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO channel_bindings"
            " (channel, external_conversation_id, conversation_id, space_id,"
            " title, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("custom", "room-1", "conversation-1", "missing", None, "2026-01-01"),
        )


def test_feedback_routing_columns_exist() -> None:
    """The correction routing columns are present after migrations."""
    connection = _connect()
    _apply(connection)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(feedback)").fetchall()
    }
    assert {
        "reporter_chat_id",
        "proposal_prompt_message_id",
        "edit_prompt_message_id",
    } <= columns


def test_qa_items_allow_one_variant_per_scope() -> None:
    """The same canonical question exists once per scope, not once globally."""
    connection = _connect()
    _apply(connection)
    sql = (
        "INSERT INTO qa_items"
        " (id, canonical_key, canonical_question, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
    )
    values = ("q1", "equipment", "?", "active", "2026-01-01", "2026-01-01")
    connection.execute(sql, values)
    connection.execute(
        "INSERT INTO qa_items"
        " (id, canonical_key, canonical_question, status, created_at, updated_at,"
        " scope_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "q2",
            "equipment",
            "?",
            "active",
            "2026-01-01",
            "2026-01-01",
            "space:sp_" + "1" * 32,
        ),
    )
    duplicate = ("q3", "equipment", "?", "active", "2026-01-01", "2026-01-01")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, duplicate)


def test_message_idempotency_key_is_unique() -> None:
    """The same (source_id, external_id) cannot be stored twice."""
    connection = _connect()
    _apply(connection)
    _seed_source_and_conversation(connection)
    sql = (
        "INSERT INTO messages"
        " (id, source_id, conversation_id, external_id, sender_hash, sender_name,"
        " sender_is_admin, sent_at, text, content_type, reply_to_message_id,"
        " created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    first = (
        "m1",
        "s1",
        "c1",
        "ext-1",
        None,
        None,
        0,
        "2026-01-01",
        "hi",
        "text",
        None,
        "2026-01-01",
    )
    connection.execute(sql, first)
    duplicate = (
        "m2",
        "s1",
        "c1",
        "ext-1",
        None,
        None,
        0,
        "2026-01-01",
        "again",
        "text",
        None,
        "2026-01-01",
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, duplicate)


def test_qa_version_history_is_append_only() -> None:
    """Superseding a version keeps both rows and links them."""
    connection = _connect()
    _apply(connection)
    connection.execute(
        "INSERT INTO qa_items"
        " (id, canonical_key, canonical_question, status, current_version_id,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("q1", "equipment", "?", "active", "v1", "2026-01-01", "2026-01-01"),
    )
    connection.execute(
        "INSERT INTO qa_versions"
        " (id, qa_id, answer, authority, confidence, origin, created_by,"
        " supersedes_version_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("v1", "q1", "a1", 60, None, "auto_generated", None, None, "2026-01-01"),
    )
    connection.execute(
        "INSERT INTO qa_versions"
        " (id, qa_id, answer, authority, confidence, origin, created_by,"
        " supersedes_version_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("v2", "q1", "a2", 100, None, "admin_approved", "admin", "v1", "2026-01-02"),
    )
    connection.execute("UPDATE qa_items SET current_version_id = 'v2' WHERE id = 'q1'")
    rows = connection.execute(
        "SELECT id, supersedes_version_id FROM qa_versions ORDER BY id"
    ).fetchall()
    assert rows == [("v1", None), ("v2", "v1")]
    current = connection.execute(
        "SELECT current_version_id FROM qa_items WHERE id = 'q1'"
    ).fetchone()
    assert current == ("v2",)
