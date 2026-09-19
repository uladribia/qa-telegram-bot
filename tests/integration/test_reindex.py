# SPDX-License-Identifier: MIT
"""Integration tests for reindexing the derived vector store from D1."""

from knowledge_bot.application.reindex import ReindexService
from knowledge_bot.infrastructure.cloudflare.d1 import D1SearchIndexSource
from knowledge_bot.ports.index import IndexableMessage, IndexableQA
from tests.fakes.ai import FakeEmbedder, FakeSearchIndexSource, FakeVectorStore
from tests.fakes.d1 import FakeD1Database


async def test_reindex_embeds_qa_and_messages_with_metadata() -> None:
    """Q&A and messages are embedded with the metadata retrieval filters use."""
    source = FakeSearchIndexSource(
        qa=[
            IndexableQA(
                version_id="v1", question="Quan?", answer="Dimarts", authority=90
            )
        ],
        messages=[
            IndexableMessage(
                message_id="m1",
                text="hola",
                source_type="telegram",
                authority=40,
            )
        ],
    )
    vectors = FakeVectorStore()
    embedder = FakeEmbedder([1.0, 0.0])
    service = ReindexService(source=source, embedder=embedder, vectors=vectors)
    report = await service.reindex()
    assert report.qa == 1
    assert report.messages == 1
    assert len(embedder.calls) == 2
    qa = vectors.records["v1"]
    assert qa.metadata["kind"] == "qa_version"
    assert qa.metadata["status"] == "active"
    assert qa.metadata["authority"] == 90
    assert qa.metadata["question"] == "Quan?"
    assert qa.metadata["text"] == "Dimarts"
    message = vectors.records["m1"]
    assert message.metadata["kind"] == "message"
    assert message.metadata["authority"] == 40


async def test_reindex_of_an_empty_source_is_a_noop() -> None:
    """Nothing to index means no embedding and no upsert."""
    vectors = FakeVectorStore()
    embedder = FakeEmbedder()
    service = ReindexService(
        source=FakeSearchIndexSource(), embedder=embedder, vectors=vectors
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
        "INSERT INTO sources VALUES"
        " ('telegram','telegram',NULL,NULL,NULL,40,0,'2026-01-01')"
    )
    connection.execute(
        "INSERT INTO conversations VALUES ('-100','telegram',NULL,NULL,'2026-01-01')"
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
        "INSERT INTO qa_items VALUES"
        " ('q1','equipment','Quan?','active','v1','2026-01-01','2026-01-01')"
    )
    connection.execute(
        "INSERT INTO qa_versions"
        " (id, qa_id, answer, authority, confidence, origin, created_by,"
        " supersedes_version_id, created_at, source_url)"
        " VALUES ('v1','q1','Dimarts',90,NULL,'web_seed',NULL,NULL,'2026-01-01',"
        "'https://x.test/#a1')"
    )
    connection.execute(
        "INSERT INTO qa_items VALUES"
        " ('q2','old','Old?','superseded','v2','2026-01-01','2026-01-01')"
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
    assert messages[0].source_type == "telegram"
    assert messages[0].author == "Ada"
    assert messages[0].authority == 40


async def test_an_approved_correction_cites_its_author_not_the_web() -> None:
    """A corrected answer is cited by its proposer and date, never a web URL."""
    database = FakeD1Database()
    connection = database.connection
    connection.execute(
        "INSERT INTO qa_items VALUES"
        " ('q1','403af1d3b03b694e','Com es diu?','active','v2','2026-01-01',"
        "'2026-01-02')"
    )
    connection.execute(
        "INSERT INTO qa_versions"
        " (id, qa_id, answer, authority, confidence, origin, created_by,"
        " supersedes_version_id, created_at, author) VALUES"
        " ('v2','q1','Resposta corregida',100,NULL,'admin_approved','Ada','v1',"
        "'2026-01-02','Ada')"
    )
    connection.commit()

    qa = await D1SearchIndexSource(database).list_qa()
    assert len(qa) == 1
    assert qa[0].url is None
    assert qa[0].author == "Ada"
    assert qa[0].date == "02/01/2026"
