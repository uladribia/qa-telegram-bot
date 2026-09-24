# SPDX-License-Identifier: MIT
"""Integration tests for the correction flow (spec §34)."""

from dataclasses import replace
from datetime import UTC, datetime

from knowledge_bot.application.feedback import (
    GROUP_SCOPE,
    FeedbackService,
    callback_action,
    callback_target,
    canonical_key_for,
)
from knowledge_bot.domain.entities import BotAnswer, QAItem, QAVersion
from knowledge_bot.domain.enums import (
    AnswerMode,
    FeedbackStatus,
    QAStatus,
)
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from tests.fakes.repositories import (
    InMemoryBotAnswerRepository,
    InMemoryConversationRepository,
    InMemoryFeedbackRepository,
    InMemoryQAEvidenceRepository,
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
)
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)
SPACE_ID = "sp_" + "1" * 32
GROUP_SCOPE_KEY = scope_for_space(SPACE_ID)


async def _service() -> tuple[
    FeedbackService,
    InMemoryBotAnswerRepository,
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
    InMemoryFeedbackRepository,
]:
    answers = InMemoryBotAnswerRepository()
    items = InMemoryQAItemRepository()
    versions = InMemoryQAVersionRepository()
    feedback = InMemoryFeedbackRepository()
    service = FeedbackService(
        answers=answers,
        feedback=feedback,
        qa_items=items,
        qa_versions=versions,
        evidence=InMemoryQAEvidenceRepository(),
        conversations=InMemoryConversationRepository(),
        clock=FrozenClock(NOW),
    )
    return service, answers, items, versions, feedback


async def _seed_answer(answers: InMemoryBotAnswerRepository) -> BotAnswer:
    answer = BotAnswer(
        id="ans:m1",
        conversation_id="-100",
        space_id=SPACE_ID,
        question="Com es demana l'equipament?",
        answer="Resposta antiga.",
        answer_mode=AnswerMode.DIRECT_QA,
        created_at=NOW,
        user_message_id="m1",
    )
    await answers.add(answer)
    return answer


def test_callback_payloads_are_parsed() -> None:
    """The callback action and target are extracted from the payload."""
    assert callback_action("feedback:start:ans:m1") == "start"
    assert callback_action("feedback:approve-global:fb:ans:m1") == "approve_global"
    assert callback_action("feedback:approve-group:fb:ans:m1") == "approve_group"
    assert callback_action("feedback:edit:fb:ans:m1") == "edit"
    assert callback_action("feedback:reject:fb:ans:m1") == "reject"
    assert callback_action("feedback:approve:fb:ans:m1") is None
    assert callback_action("other:thing") is None
    assert callback_target("feedback:approve-global:fb:ans:m1") == "fb:ans:m1"


async def test_feedback_stores_the_cited_qa_item_id() -> None:
    """A direct answer resolves its version to the semantic Q&A item."""
    service, answers, items, versions, _ = await _service()
    answer = await _seed_answer(answers)
    await answers.add(replace(answer, qa_version_id="qav-web-1"))
    await items.add(
        QAItem(
            id="qa-item",
            canonical_key=canonical_key_for(answer.question),
            canonical_question=answer.question,
            status=QAStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            current_version_id="qav-web-1",
        )
    )
    await versions.add(
        QAVersion(
            id="qav-web-1",
            qa_id="qa-item",
            answer=answer.answer,
            authority=90,
            origin="web_seed",
            created_at=NOW,
        )
    )

    started = await service.start(answer.id, None)

    assert started is not None and started.qa_id == "qa-item"


async def test_full_correction_flow_creates_a_new_authoritative_version() -> None:
    """Start -> propose -> approve supersedes the old version."""
    service, answers, items, versions, feedback = await _service()
    await _seed_answer(answers)

    started = await service.start("ans:m1", "reporter-hash")
    assert started is not None
    assert started.status is FeedbackStatus.AWAITING_PROPOSAL

    proposed = await service.propose(started.id, "La llista la passa l'entrenador.")
    assert proposed is not None
    assert proposed.status is FeedbackStatus.PENDING_ADMIN

    request = await service.correction_request(started.id)
    assert request is not None
    assert request.question == "Com es demana l'equipament?"
    assert request.current_answer == "Resposta antiga."
    assert request.proposed_answer == "La llista la passa l'entrenador."
    # The group of origin is always visible: chat id when unregistered.
    assert request.group_label == "-100"
    assert request.current_origin is None

    version = await service.approve(started.id, GROUP_SCOPE)
    assert version is not None
    assert version.authority == 100
    assert version.origin == "human_approved"

    stored_feedback = await feedback.get(started.id)
    assert stored_feedback is not None
    assert stored_feedback.status is FeedbackStatus.APPROVED

    item = await items.get(version.qa_id)
    assert item is not None
    assert item.current_version_id == version.id
    assert item.status is QAStatus.ACTIVE
    assert item.scope_key == GROUP_SCOPE_KEY
    assert item.canonical_key == canonical_key_for("Com es demana l'equipament?")

    assert await versions.get(version.id) is not None


async def test_approving_supersedes_a_seeded_web_version() -> None:
    """A correction to a web-seeded Q&A creates a local override."""
    service, answers, items, versions, _ = await _service()
    await _seed_answer(answers)
    key = canonical_key_for("Com es demana l'equipament?")
    qa_id = f"qa-{key}"
    await items.add(
        QAItem(
            id=qa_id,
            canonical_key=key,
            canonical_question="Com es demana l'equipament?",
            status=QAStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            current_version_id="qav-web-1",
        )
    )
    await versions.add(
        QAVersion(
            id="qav-web-1",
            qa_id=qa_id,
            answer="Resposta del web.",
            authority=90,
            origin="web_seed",
            created_at=NOW,
        )
    )
    started = await service.start("ans:m1", None)
    assert started is not None
    await service.propose(started.id, "Resposta corregida.")
    version = await service.approve(started.id, GLOBAL_SCOPE)
    assert version is not None
    assert version.supersedes_version_id == "qav-web-1"
    assert version.qa_id == qa_id
    assert await versions.get("qav-web-1") is not None
    assert await versions.get(version.id) is not None
    item = await items.get(qa_id)
    assert item is not None
    assert item.current_version_id == version.id


async def test_resolved_feedback_cannot_be_decided_twice() -> None:
    """A repeated approval or rejection creates no second decision."""
    service, answers, items, _, _ = await _service()
    await _seed_answer(answers)
    started = await service.start("ans:m1", None)
    assert started is not None
    await service.propose(started.id, "Resposta corregida.")
    approved = await service.approve(started.id, GROUP_SCOPE)
    assert approved is not None

    assert await service.approve(started.id, GROUP_SCOPE) is None
    assert await service.reject(started.id) is None
    item = await items.get(approved.qa_id)
    assert item is not None
    assert item.current_version_id == approved.id


async def test_admin_edit_takes_precedence_over_the_proposal() -> None:
    """The admin-edited text wins over the reporter's proposal."""
    service, answers, _, _, _ = await _service()
    await _seed_answer(answers)
    started = await service.start("ans:m1", None)
    assert started is not None
    await service.propose(started.id, "proposta del reporter")
    await service.admin_edit(started.id, "text editat per l'admin")
    version = await service.approve(started.id, GROUP_SCOPE)
    assert version is not None
    assert version.answer == "text editat per l'admin"


async def test_reject_leaves_knowledge_untouched() -> None:
    """Rejecting a proposal changes no Q&A."""
    service, answers, items, _, _ = await _service()
    await _seed_answer(answers)
    started = await service.start("ans:m1", None)
    assert started is not None
    await service.propose(started.id, "proposta")
    rejected = await service.reject(started.id)
    assert rejected is not None
    assert rejected.status is FeedbackStatus.REJECTED
    assert (
        await items.get_by_canonical_key(
            canonical_key_for("Com es demana l'equipament?")
        )
        is None
    )


async def test_starting_feedback_for_an_unknown_answer_is_a_noop() -> None:
    """An unknown answer id produces no feedback."""
    service, _, _, _, _ = await _service()
    assert await service.start("nope", None) is None


async def test_approving_as_group_and_global_builds_both_variants() -> None:
    """Each scope approval lands in its own item; neither overwrites the other."""
    service, answers, items, versions, _feedback = await _service()
    await _seed_answer(answers)
    started = await service.start("ans:m1", None)
    assert started is not None
    await service.propose(started.id, "Resposta del grup.")

    group_version = await service.approve(started.id, GROUP_SCOPE)
    assert group_version is not None
    assert group_version.answer == "Resposta del grup."
    group_item = await items.get(group_version.qa_id)
    assert group_item is not None
    assert group_item.scope_key == GROUP_SCOPE_KEY
    # The global scope has no item yet: the group variant did not leak.
    assert (
        await items.get_by_canonical_key(
            canonical_key_for("Com es demana l'equipament?"), GLOBAL_SCOPE
        )
        is None
    )

    # A second correction (on another answer) approved as global creates the
    # global variant.
    previous = await answers.get("ans:m1")
    assert previous is not None
    await answers.add(
        replace(
            previous,
            id="ans:m2",
            user_message_id="m2",
        )
    )
    started2 = await service.start("ans:m2", None)
    assert started2 is not None
    await service.propose(started2.id, "Resposta global.")
    global_version = await service.approve(started2.id, GLOBAL_SCOPE)
    assert global_version is not None
    global_item = await items.get(global_version.qa_id)
    assert global_item is not None
    assert global_item.scope_key == GLOBAL_SCOPE
    assert global_item.id != group_item.id
    # The group variant keeps its own answer.
    group_after = await items.get(group_item.id)
    assert group_after is not None
    assert group_after.current_version_id == group_version.id
    assert await versions.get(global_version.id) is not None
