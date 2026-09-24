# SPDX-License-Identifier: MIT
"""Integration tests for reindexing the derived vector store from D1."""

from datetime import UTC, datetime

from knowledge_bot.application.reindex import ReindexService
from knowledge_bot.infrastructure.cloudflare.d1 import D1SearchIndexSource
from knowledge_bot.ports.index import IndexableMessage, IndexableQA
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import (
    FakeEmbedder,
    FakeSearchIndexSource,
    FakeVectorStore,
    InMemorySearchProjectionRepository,
)
from tests.fakes.d1 import FakeD1Database
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def test_reindex_embeds_qa_and_messages_with_metadata() -> None:
    """Q&A and messages are embedded with the metadata retrieval filters use."""
    source = FakeSearchIndexSource(
        qa=[
            IndexableQA(
                qa_item_id="q1",
                version_id="v1",
                question="Quan?",
                answer="Dimarts",
                authority=90,
                canonical_key="equipment",
            )
        ],
        messages=[
            IndexableMessage(
                message_id="m1",
                text="hola",
                source_kind="telegram",
                authority=40,
                conversation_id="-100",
                scope_key="space:" + "sp_" + "1" * 32,
            )
        ],
    )
    vectors = FakeVectorStore()
    embedder = FakeEmbedder([1.0, 0.0])
    service = ReindexService(
        source=source,
        embedder=embedder,
        vectors=vectors,
        manifest=InMemorySearchProjectionRepository(),
        clock=FrozenClock(NOW),
    )
    report = await service.reindex()
    assert report.qa == 1
    assert report.messages == 1
    assert len(embedder.calls) == 2
    qa = vectors.records["qa:q1"]
    assert qa.metadata["kind"] == "qa"
    assert qa.metadata["status"] == "active"
    assert qa.metadata["authority"] == 90
    assert qa.metadata["scope_key"] == "global"
    assert qa.metadata["question"] == "Quan?"
    assert qa.metadata["text"] == "Dimarts"
    message = vectors.records["msg:m1"]
    assert message.metadata["kind"] == "message_evidence"
    assert message.metadata["authority"] == 40
    assert message.metadata["scope_key"] == "space:" + "sp_" + "1" * 32


async def test_qa_version_update_reuses_one_stable_item_vector() -> None:
    """A new current version replaces the same vector instead of adding a stale one."""
    v1 = IndexableQA(
        qa_item_id="q1",
        version_id="v1",
        question="Quan?",
        answer="Dimarts",
        authority=90,
        canonical_key="equipment",
    )
    v2 = IndexableQA(
        qa_item_id="q1",
        version_id="v2",
        question="Quan?",
        answer="Dijous",
        authority=100,
        canonical_key="equipment",
    )
    source = FakeSearchIndexSource(qa=[v1])
    vectors = FakeVectorStore()
    manifest = InMemorySearchProjectionRepository()
    service = ReindexService(
        source=source,
        embedder=FakeEmbedder(),
        vectors=vectors,
        manifest=manifest,
        clock=FrozenClock(NOW),
    )
    await service.reindex_qa_version("v1")
    source.qa = [v2]
    await service.reindex_qa_version("v2")

    assert set(vectors.records) == {"qa:q1"}
    assert vectors.records["qa:q1"].metadata["version_id"] == "v2"
    assert vectors.records["qa:q1"].metadata["text"] == "Dijous"
    assert await manifest.list_vector_ids() == ["qa:q1"]


async def test_rebuild_deletes_the_known_projection_before_reindexing() -> None:
    """A rebuild never relies on upsert-only semantics."""
    vectors = FakeVectorStore()
    old = VectorRecord(
        id="qa:old",
        values=[1.0, 0.0],
        metadata={"kind": "qa", "object_id": "old"},
    )
    await vectors.upsert([old])
    manifest = InMemorySearchProjectionRepository()
    await manifest.record([old], NOW)
    service = ReindexService(
        source=FakeSearchIndexSource(),
        embedder=FakeEmbedder(),
        vectors=vectors,
        manifest=manifest,
        clock=FrozenClock(NOW),
    )

    await service.rebuild()

    assert vectors.records == {}
    assert await manifest.list_vector_ids() == []


async def test_reindex_of_an_empty_source_is_a_noop() -> None:
    """Nothing to index means no embedding and no upsert."""
    vectors = FakeVectorStore()
    embedder = FakeEmbedder()
    service = ReindexService(
        source=FakeSearchIndexSource(),
        embedder=embedder,
        vectors=vectors,
        manifest=InMemorySearchProjectionRepository(),
        clock=FrozenClock(NOW),
    )
    report = await service.reindex()
    assert report.qa == 0
    assert report.messages == 0
    assert embedder.calls == []
    assert vectors.records == {}


async def test_d1_source_reads_only_current_active_qa_and_text_messages() -> None:
    """The D1 source joins current active versions and skips text-less messages."""
    database = FakeD1Database()
    connection = database.connection
    connection.execute(
        "INSERT INTO sources (id, source_type, external_ref, title,"
        " canonical_url, authority, is_mutable, created_at) VALUES"
        " ('telegram','telegram',NULL,NULL,NULL,40,0,'2026-01-01')"
    )
    connection.execute(
        "INSERT INTO conversations"
        " (id, source_id, external_id, title, created_at)"
        " VALUES ('-100','telegram',NULL,NULL,'2026-01-01')"
    )
    connection.execute(
        "INSERT INTO messages"
        " (id, source_id, conversation_id, external_id, sender_hash, sender_name,"
        " sender_is_admin, sent_at, text, content_type, reply_to_message_id,"
        " created_at)"
        " VALUES ('m1','telegram','-100','-100:1',NULL,'Ada',0,'2026-01-01','hola',"
        "'text',NULL,'2026-01-01')"
    )
    connection.execute(
        "INSERT INTO messages"
        " (id, source_id, conversation_id, external_id, sender_hash, sender_name,"
        " sender_is_admin, sent_at, text, content_type, reply_to_message_id,"
        " created_at)"
        " VALUES ('m2','telegram','-100','-100:2',NULL,NULL,0,'2026-01-01',NULL,"
        "'image',NULL,'2026-01-01')"
    )
    connection.execute(
        "INSERT INTO qa_items (id, canonical_key, canonical_question, status,"
        " current_version_id, created_at, updated_at, scope_key) VALUES"
        " ('q1','equipment','Quan?','active','v1','2026-01-01','2026-01-01','global')"
    )
    connection.execute(
        "INSERT INTO qa_versions"
        " (id, qa_id, answer, authority, confidence, origin, created_by,"
        " supersedes_version_id, created_at, source_url, source_anchor)"
        " VALUES ('v1','q1','Dimarts',90,NULL,'web_seed',NULL,NULL,'2026-01-01',"
        "'https://x.test/','a1')"
    )
    connection.execute(
        "INSERT INTO qa_items (id, canonical_key, canonical_question, status,"
        " current_version_id, created_at, updated_at, scope_key) VALUES"
        " ('q2','old','Old?','superseded','v2','2026-01-01','2026-01-01','global')"
    )
    connection.execute(
        "INSERT INTO qa_versions"
        " (id, qa_id, answer, authority, confidence, origin, created_by,"
        " supersedes_version_id, created_at) VALUES"
        " ('v2','q2','Old',30,NULL,'web_seed',NULL,NULL,'2026-01-01')"
    )
    connection.commit()

    source = D1SearchIndexSource(database)
    qa = await source.list_qa()
    messages = await source.list_messages()
    assert [item.version_id for item in qa] == ["v1"]
    assert qa[0].question == "Quan?"
    assert qa[0].authority == 90
    assert qa[0].url == "https://x.test/#a1"
    assert [message.message_id for message in messages] == ["m1"]
    assert messages[0].source_kind == "telegram"
    assert messages[0].author == "Ada"
    assert messages[0].authority == 40


async def test_an_approved_correction_cites_its_author_not_the_web() -> None:
    """A corrected answer is cited by its proposer and date, never a web URL."""
    database = FakeD1Database()
    connection = database.connection
    connection.execute(
        "INSERT INTO qa_items (id, canonical_key, canonical_question, status,"
        " current_version_id, created_at, updated_at, scope_key) VALUES"
        " ('q1','403af1d3b03b694e','Com es diu?','active','v2','2026-01-01',"
        "'2026-01-02','global')"
    )
    connection.execute(
        "INSERT INTO qa_versions"
        " (id, qa_id, answer, authority, confidence, origin, created_by,"
        " supersedes_version_id, created_at, author) VALUES"
        " ('v2','q1','Resposta corregida',100,NULL,'human_approved','Ada','v1',"
        "'2026-01-02','Ada')"
    )
    connection.commit()

    qa = await D1SearchIndexSource(database).list_qa()
    assert len(qa) == 1
    assert qa[0].url is None
    assert qa[0].author == "Ada"
    assert qa[0].date == "02/01/2026"
