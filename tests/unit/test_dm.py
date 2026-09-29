# SPDX-License-Identifier: MIT
"""Tests for collapsing and labelling the answers to a private question."""

from datetime import UTC, datetime

from knowledge_bot.application.answer_policy import AnswerPolicy
from knowledge_bot.application.answer_question import ABSTENTION_TEXT, AnswerService
from knowledge_bot.application.dm import AnswerBundle, DirectAnswerService
from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.domain.entities import ChannelBinding
from knowledge_bot.domain.enums import ContentType
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.models.common import SourceDescriptor
from knowledge_bot.models.messages import NormalizedMessage
from knowledge_bot.ports.generator import GenerationOutput
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import FakeEmbedder, FakeGenerator, FakeVectorStore
from tests.fakes.repositories import InMemoryBotAnswerRepository
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)
SPACE_A = "sp_" + "1" * 32
SPACE_B = "sp_" + "2" * 32


def _message(text: str = "Quan entrenen?") -> NormalizedMessage:
    return NormalizedMessage(
        id="111:90",
        source=SourceDescriptor(
            id="src:telegram:runtime", kind="telegram", authority=40
        ),
        conversation_id="111",
        content_type=ContentType.TEXT,
        timestamp=NOW,
        text=text,
        sender_is_admin=False,
    )


CHAT_A = "-5428209312"
CHAT_B = "-1004319238076"


def _binding(space_id: str, title: str, chat_id: str) -> ChannelBinding:
    return ChannelBinding(
        channel="telegram",
        external_conversation_id=chat_id,
        conversation_id=chat_id,
        space_id=space_id,
        created_at=NOW,
        title=title,
    )


def _qa(vector_id: str, scope_key: str, key: str) -> VectorRecord:
    return VectorRecord(
        id=vector_id,
        values=[1.0, 0.0],
        metadata={
            "kind": "qa",
            "object_id": key,
            "version_id": "qav:1",
            "canonical_key": key,
            "status": "active",
            "scope_key": scope_key,
            "text": "Els dimarts a les sis.",
            "authority": 90,
            "question": f"Quan entrenen? {key}",
        },
    )


async def _service(records: list[VectorRecord]) -> DirectAnswerService:
    vectors = FakeVectorStore()
    await vectors.upsert(records)
    answer = AnswerService(
        RetrievalService(FakeEmbedder([1.0, 0.0]), vectors),
        FakeGenerator(),
        InMemoryBotAnswerRepository(),
        FrozenClock(NOW),
        policy=AnswerPolicy(floor=0.35),
    )
    return DirectAnswerService(answer=answer)


async def test_the_same_answer_from_two_groups_is_one_delivery() -> None:
    """Identical words and identical sources are one answer, not one per group."""
    service = await _service([_qa("qa:global", GLOBAL_SCOPE, "horari")])
    bundles = await _run(
        service, [_binding(SPACE_A, "Alpha", CHAT_A), _binding(SPACE_B, "Beta", CHAT_B)]
    )
    assert len(bundles) == 1
    assert bundles[0].answer_ids == (
        f"ans:111:90:{CHAT_A}",
        f"ans:111:90:{CHAT_B}",
    )
    assert "\U0001f310" not in bundles[0].text


async def test_a_group_own_answer_is_delivered_apart_from_the_club_s() -> None:
    """Different sources stay separate even when the wording is the same."""
    service = await _service(
        [
            _qa("qa:global", GLOBAL_SCOPE, "horari"),
            _qa("qa:alpha", scope_for_space(SPACE_A), "material"),
        ]
    )
    bundles = await _run(
        service, [_binding(SPACE_A, "Alpha", CHAT_A), _binding(SPACE_B, "Beta", CHAT_B)]
    )
    assert len(bundles) == 2
    texts = {bundle.representative_answer_id: bundle.text for bundle in bundles}
    assert texts[f"ans:111:90:{CHAT_A}"].startswith(
        "\U0001f310 Global \u00b7 \U0001f465 Alpha"
    )
    assert texts[f"ans:111:90:{CHAT_B}"].startswith("\U0001f310 Global")


async def test_global_comes_first_and_groups_keep_the_given_order() -> None:
    """The caller resolves the display order; the service keeps to it.

    Both blocks here lead with global knowledge, so what separates them is the
    group that differs, and that is the order they were resolved in.
    """
    service = await _service(
        [
            _qa("qa:global", GLOBAL_SCOPE, "horari"),
            _qa("qa:zebra", scope_for_space(SPACE_A), "zebra"),
            _qa("qa:alpha", scope_for_space(SPACE_B), "alpha"),
        ]
    )
    bundles = await _run(
        service,
        [_binding(SPACE_B, "Alpha", CHAT_B), _binding(SPACE_A, "Zebra", CHAT_A)],
    )
    assert [bundle.representative_answer_id for bundle in bundles] == [
        f"ans:111:90:{CHAT_B}",
        f"ans:111:90:{CHAT_A}",
    ]


async def test_the_local_answer_represents_a_collapsed_block() -> None:
    """Flagging a collapsed answer should land on the group that made it."""
    service = await _service(
        [
            _qa("qa:global", GLOBAL_SCOPE, "horari"),
            _qa("qa:local", scope_for_space(SPACE_A), "horari"),
        ]
    )
    bundles = await _run(service, [_binding(SPACE_A, "Alpha", CHAT_A)])
    assert len(bundles) == 1
    assert bundles[0].representative_answer_id == f"ans:111:90:{CHAT_A}"


async def test_an_asker_with_no_group_is_answered_globally() -> None:
    """The allowlist still works, and searches only what there is."""
    service = await _service([_qa("qa:global", GLOBAL_SCOPE, "horari")])
    bundles = await _run(service, [])
    assert [bundle.representative_answer_id for bundle in bundles] == [
        "ans:111:90:global"
    ]


async def test_an_unanswerable_question_gets_an_abstention() -> None:
    """A question that was asked is owed an answer, even when it is "I don't know".

    What it is not owed is a group heading: nothing in any group had a view.
    """
    service = await _service([])
    bundles = await _run(service, [_binding(SPACE_A, "Alpha", CHAT_A)])
    assert len(bundles) == 1
    assert bundles[0].text == ABSTENTION_TEXT
    assert "\U0001f465" not in bundles[0].text


async def test_an_abstention_sorts_last_and_carries_no_heading() -> None:
    """A group heading on an abstention would blame a group for nothing."""
    service = await _service([_qa("qa:global", GLOBAL_SCOPE, "horari")])
    bundles = await _run(
        service,
        [_binding(SPACE_A, "Alpha", CHAT_A)],
        generator=FakeGenerator(GenerationOutput(status="insufficient")),
    )
    assert len(bundles) == 1
    assert "\U0001f310" not in bundles[0].text
    assert "\U0001f465" not in bundles[0].text


async def test_a_message_with_no_question_yields_nothing() -> None:
    """Nothing to ask is nothing to answer."""
    service = await _service([_qa("qa:global", GLOBAL_SCOPE, "horari")])
    bundles = await service.bundles_for(
        _message("   "), served=[_binding(SPACE_A, "Alpha", CHAT_A)]
    )
    assert bundles == ()


async def _run(
    service: DirectAnswerService,
    served: list[ChannelBinding],
    *,
    generator: FakeGenerator | None = None,
) -> tuple[AnswerBundle, ...]:
    """Answer one private message in the given scopes."""
    if generator is not None:
        service = DirectAnswerService(
            answer=AnswerService(
                service.answer.retrieval,
                generator,
                service.answer.answers,
                FrozenClock(NOW),
                policy=AnswerPolicy(floor=0.35),
            )
        )
    return await service.bundles_for(_message(), served=served)
