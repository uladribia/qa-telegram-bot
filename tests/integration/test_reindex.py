# SPDX-License-Identifier: MIT
"""Integration tests for reindexing the derived vector store."""

from datetime import UTC, datetime

from knowledge_bot.application.indexing import SearchProjectionService
from knowledge_bot.application.reindex import ReindexService
from knowledge_bot.infrastructure.cloudflare.d1 import D1SearchIndexSource
from knowledge_bot.ports.index import IndexableMessage, IndexableQA
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import (
    FakeEmbedder,
    FakeLexicalIndex,
    FakeSearchIndexSource,
    FakeVectorStore,
    InMemorySearchProjectionRepository,
)
from tests.fakes.d1 import FakeD1Database
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _service(
    source: FakeSearchIndexSource,
    vectors: FakeVectorStore,
    manifest: InMemorySearchProjectionRepository,
) -> ReindexService:
    projector = SearchProjectionService(
        source,
        FakeEmbedder([1.0, 0.0]),
        vectors,
        FakeLexicalIndex(),
        manifest,
        FrozenClock(NOW),
    )
    return ReindexService(source, projector, FrozenClock(NOW))


async def test_reindex_embeds_qa_and_messages_with_metadata() -> None:
    """Q&A and messages use stable ids and retrieval metadata."""
    source = FakeSearchIndexSource(
        qa=[IndexableQA("q1", "v1", "Quan?", "Dimarts", 90, "equipment")],
        messages=[
            IndexableMessage(
                "m1", "hola", "telegram", 40, "-100", "space:" + "sp_" + "1" * 32
            )
        ],
    )
    vectors = FakeVectorStore()
    manifest = InMemorySearchProjectionRepository()
    service = _service(source, vectors, manifest)
    report = await service.reindex()
    assert report.qa == 1
    assert report.messages == 1
    assert vectors.records["qa:q1"].metadata["kind"] == "qa"
    assert vectors.records["msg:m1"].metadata["kind"] == "message_evidence"
    assert await manifest.list_vector_ids() == ["msg:m1", "qa:q1"]


async def test_qa_version_update_reuses_one_stable_item_vector() -> None:
    """A new current version replaces the same stable vector."""
    source = FakeSearchIndexSource(
        qa=[IndexableQA("q1", "v1", "Quan?", "Dimarts", 90, "equipment")]
    )
    vectors = FakeVectorStore()
    manifest = InMemorySearchProjectionRepository()
    service = _service(source, vectors, manifest)
    await service.reindex_qa_version("v1")
    source.qa = [IndexableQA("q1", "v2", "Quan?", "Dijous", 100, "equipment")]
    await service.reindex_qa_version("v2")
    assert set(vectors.records) == {"qa:q1"}
    assert vectors.records["qa:q1"].metadata["version_id"] == "v2"
    assert vectors.records["qa:q1"].metadata["text"] == "Dijous"


async def test_cleanup_deletes_known_projection_without_embedding() -> None:
    """Projection cleanup deletes known vectors without embedding."""
    vectors = FakeVectorStore()
    manifest = InMemorySearchProjectionRepository()
    await vectors.upsert(
        [VectorRecord("qa:old", [1.0, 0.0], {"kind": "qa", "object_id": "old"})]
    )
    await manifest.record([next(iter(vectors.records.values()))], NOW)
    service = _service(FakeSearchIndexSource(), vectors, manifest)
    await service.cleanup_projection()
    assert vectors.records == {}
    assert await manifest.list_vector_ids() == []


async def test_reindex_of_an_empty_source_is_a_noop() -> None:
    """An empty source performs no projection."""
    vectors = FakeVectorStore()
    service = _service(
        FakeSearchIndexSource(), vectors, InMemorySearchProjectionRepository()
    )
    report = await service.reindex()
    assert report.qa == 0
    assert report.messages == 0
    assert vectors.records == {}


async def test_d1_source_reads_only_current_active_qa_and_text_messages() -> None:
    """D1 source excludes superseded Q&A and text-less messages."""
    database = FakeD1Database()
    connection = database.connection
    connection.execute(
        "INSERT INTO sources (id, source_type, external_ref, title, canonical_url, authority, is_mutable, created_at) VALUES ('telegram','telegram',NULL,NULL,NULL,40,0,'2026-01-01')"  # noqa: E501
    )
    connection.execute(
        "INSERT INTO conversations (id, source_id, external_id, title, created_at) VALUES ('-100','telegram',NULL,NULL,'2026-01-01')"  # noqa: E501
    )
    connection.execute(
        "INSERT INTO messages (id, source_id, conversation_id, external_id, sender_hash, sender_name, sender_is_admin, sender_authority, sent_at, text, content_type, reply_to_message_id, created_at, index_status) VALUES ('m1','telegram','-100','-100:1',NULL,'Ada',0,NULL,'2026-01-01','hola','text',NULL,'2026-01-01','indexed')"  # noqa: E501
    )
    connection.execute(
        "INSERT INTO messages (id, source_id, conversation_id, external_id, sender_hash, sender_name, sender_is_admin, sender_authority, sent_at, text, content_type, reply_to_message_id, created_at, index_status) VALUES ('m2','telegram','-100','-100:2',NULL,NULL,0,NULL,'2026-01-01',NULL,'image',NULL,'2026-01-01','not_indexed')"  # noqa: E501
    )
    connection.execute(
        "INSERT INTO qa_items (id, canonical_key, canonical_question, status, current_version_id, created_at, updated_at, scope_key) VALUES ('q1','equipment','Quan?','active','v1','2026-01-01','2026-01-01','global')"  # noqa: E501
    )
    connection.execute(
        "INSERT INTO qa_versions (id, qa_id, answer, authority, confidence, origin, created_by, supersedes_version_id, created_at, source_url, source_anchor) VALUES ('v1','q1','Dimarts',90,NULL,'web_seed',NULL,NULL,'2026-01-01','https://x.test/','a1')"  # noqa: E501
    )
    connection.execute(
        "INSERT INTO qa_items (id, canonical_key, canonical_question, status, current_version_id, created_at, updated_at, scope_key) VALUES ('q2','old','Old?','superseded','v2','2026-01-01','2026-01-01','global')"  # noqa: E501
    )
    connection.execute(
        "INSERT INTO qa_versions (id, qa_id, answer, authority, confidence, origin, created_by, supersedes_version_id, created_at) VALUES ('v2','q2','Old',30,NULL,'web_seed',NULL,NULL,'2026-01-01')"  # noqa: E501
    )
    connection.commit()
    source = D1SearchIndexSource(database)
    qa = await source.list_qa()
    messages = await source.list_messages()
    assert [item.version_id for item in qa] == ["v1"]
    assert qa[0].url == "https://x.test/#a1"
    assert [message.message_id for message in messages] == ["m1"]
    assert messages[0].authority == 40
