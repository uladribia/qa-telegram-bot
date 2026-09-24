# SPDX-License-Identifier: MIT
"""Two-group correction and retrieval scenario."""

import asyncio
from datetime import UTC, datetime

from knowledge_bot.contracts.api import AskQuestionRequest
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.enums import ReviewAction
from knowledge_bot.domain.identity import canonical_key_for
from knowledge_bot.domain.scope import scope_for_space
from knowledge_bot.ports.index import IndexableQA
from tests.fakes.ai import FakeSearchIndexSource
from tests.fakes.context import SPACE_A, SPACE_B, build_test_context

QUESTION = "Quina és la resposta local del grup?"


def _entry(answer: str, suffix: str) -> SeedQA:
    return SeedQA(
        source_url=f"https://local.invalid/{suffix}",
        source_kind="local_test",
        source_authority=90,
        section="Scenario",
        question=QUESTION,
        answer=answer,
        status="published",
        retrieved_at=datetime(2026, 9, 24, tzinfo=UTC),
        source_anchor=suffix,
    )


def test_two_groups_keep_different_local_corrections() -> None:
    """Correct the same question independently in two logical spaces."""
    context, _ = build_test_context()

    async def run() -> None:
        for space_id, answer in (
            (SPACE_A, "Resposta inicial A."),
            (SPACE_B, "Resposta inicial B."),
        ):
            created, _, _, _, versions = await context.seed.seed_qa(
                [_entry(answer, space_id)], scope=scope_for_space(space_id)
            )
            assert created == 1
            source = context.reindex.source
            assert isinstance(source, FakeSearchIndexSource)
            item = await context.feedback.qa_items.get_by_canonical_key(
                canonical_key_for(QUESTION), scope_for_space(space_id)
            )
            assert item is not None
            version = await context.feedback.qa_versions.get(versions[0])
            assert version is not None
            source.qa.append(
                IndexableQA(
                    qa_item_id=item.id,
                    version_id=version.id,
                    question=QUESTION,
                    answer=version.answer,
                    authority=version.authority,
                    canonical_key=item.canonical_key,
                    scope_key=item.scope_key,
                )
            )
            assert await context.reindex.reindex_qa_version(versions[0])

        initial_a = await context.answer.answer_request(
            AskQuestionRequest(
                request_id="scenario-a-initial", space_id=SPACE_A, question=QUESTION
            )
        )
        initial_b = await context.answer.answer_request(
            AskQuestionRequest(
                request_id="scenario-b-initial", space_id=SPACE_B, question=QUESTION
            )
        )
        assert initial_a.answer == "Resposta inicial A."
        assert initial_b.answer == "Resposta inicial B."

        for answer, correction in (
            (initial_a, "Resposta corregida A."),
            (initial_b, "Resposta corregida B."),
        ):
            feedback = await context.feedback.start(answer.answer_id, "telegram:user-a")
            assert feedback is not None
            proposed = await context.feedback.propose(
                feedback.id, correction, "telegram:user-a", "User A"
            )
            assert proposed is not None
            version = await context.feedback.approve(
                feedback.id, ReviewAction.APPROVE_LOCAL
            )
            assert version is not None
            source = context.reindex.source
            assert isinstance(source, FakeSearchIndexSource)
            item = await context.feedback.qa_items.get(version.qa_id)
            assert item is not None
            source.qa.append(
                IndexableQA(
                    qa_item_id=item.id,
                    version_id=version.id,
                    question=QUESTION,
                    answer=version.answer,
                    authority=version.authority,
                    canonical_key=item.canonical_key,
                    scope_key=item.scope_key,
                )
            )
            assert await context.reindex.reindex_qa_version(version.id)

        corrected_a = await context.answer.answer_request(
            AskQuestionRequest(
                request_id="scenario-a-corrected", space_id=SPACE_A, question=QUESTION
            )
        )
        corrected_b = await context.answer.answer_request(
            AskQuestionRequest(
                request_id="scenario-b-corrected", space_id=SPACE_B, question=QUESTION
            )
        )
        assert corrected_a.answer == "Resposta corregida A."
        assert corrected_b.answer == "Resposta corregida B."
        assert corrected_a.answer != corrected_b.answer

    asyncio.run(run())
