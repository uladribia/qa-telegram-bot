# SPDX-License-Identifier: MIT
"""Integration tests for principal-based correction flow."""

from dataclasses import replace
from datetime import UTC, datetime

from knowledge_bot.application.feedback import (
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
    ReviewAction,
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
from tests.fakes.transactions import InMemoryCorrectionCommitStore

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)
SPACE_ID = "sp_" + "1" * 32
GROUP_SCOPE_KEY = scope_for_space(SPACE_ID)
PRINCIPAL = "telegram:reporter"


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
    return (
        FeedbackService(
            answers,
            feedback,
            items,
            versions,
            InMemoryConversationRepository(),
            InMemoryCorrectionCommitStore(
                items, versions, InMemoryQAEvidenceRepository(), feedback
            ),
            FrozenClock(NOW),
        ),
        answers,
        items,
        versions,
        feedback,
    )


async def _seed_answer(answers: InMemoryBotAnswerRepository) -> BotAnswer:
    answer = BotAnswer(
        "ans:m1",
        "-100",
        "Com es demana l'equipament?",
        "Resposta antiga.",
        AnswerMode.DIRECT_QA,
        NOW,
        space_id=SPACE_ID,
        user_message_id="m1",
    )
    await answers.add(answer)
    return answer


def test_callback_payloads_are_parsed() -> None:
    """Callback actions and targets are parsed."""
    assert callback_action("feedback:start:ans:m1") == "start"
    assert callback_action("feedback:approve-global:fb:ans:m1") == "approve_global"
    assert callback_action("feedback:approve-group:fb:ans:m1") == "approve_group"
    assert callback_action("feedback:edit:fb:ans:m1") == "edit"
    assert callback_action("feedback:reject:fb:ans:m1") == "reject"
    assert callback_action("feedback:approve:fb:ans:m1") is None
    assert callback_target("feedback:approve-global:fb:ans:m1") == "fb:ans:m1"


async def test_feedback_stores_cited_item_and_principal() -> None:
    """A direct answer resolves its version and stores the reporter principal."""
    service, answers, items, versions, _ = await _service()
    answer = await _seed_answer(answers)
    await answers.add(replace(answer, qa_version_id="qav-web-1"))
    key = canonical_key_for(answer.question)
    await items.add(
        QAItem(
            "qa-item",
            key,
            answer.question,
            QAStatus.ACTIVE,
            NOW,
            NOW,
            current_version_id="qav-web-1",
        )
    )
    await versions.add(
        QAVersion("qav-web-1", "qa-item", answer.answer, 90, "web_seed", NOW)
    )
    started = await service.start(answer.id, PRINCIPAL)
    assert started is not None
    assert started.qa_id == "qa-item"
    assert started.reporter_principal_id == PRINCIPAL


async def test_full_correction_flow_creates_authoritative_local_version() -> None:
    """Start, propose, and local approval create a scoped version."""
    service, answers, items, versions, feedback = await _service()
    await _seed_answer(answers)
    started = await service.start("ans:m1", PRINCIPAL)
    assert started is not None
    proposed = await service.propose(
        started.id, "La llista la passa l'entrenador.", PRINCIPAL
    )
    assert proposed is not None and proposed.status is FeedbackStatus.PENDING_REVIEW
    request = await service.correction_request(started.id)
    assert request is not None and request.group_label == "-100"
    version = await service.approve(started.id, ReviewAction.APPROVE_LOCAL)
    assert version is not None and version.authority == 100
    item = await items.get(version.qa_id)
    assert item is not None and item.scope_key == GROUP_SCOPE_KEY
    stored_feedback = await feedback.get(started.id)
    assert (
        stored_feedback is not None
        and stored_feedback.status is FeedbackStatus.APPROVED
    )
    assert await versions.get(version.id) is not None


async def test_resolved_feedback_cannot_be_decided_twice() -> None:
    """A repeated approval or rejection creates no second decision."""
    service, answers, _items, _, _ = await _service()
    await _seed_answer(answers)
    started = await service.start("ans:m1", PRINCIPAL)
    assert started is not None
    await service.propose(started.id, "Resposta corregida.", PRINCIPAL)
    approved = await service.approve(started.id, ReviewAction.APPROVE_LOCAL)
    assert approved is not None
    assert await service.approve(started.id, ReviewAction.APPROVE_LOCAL) is None
    assert await service.reject(started.id) is None


async def test_admin_edit_takes_precedence() -> None:
    """The edited text wins over the reporter proposal."""
    service, answers, _, _, _ = await _service()
    await _seed_answer(answers)
    started = await service.start("ans:m1", PRINCIPAL)
    assert started is not None
    await service.propose(started.id, "proposta", PRINCIPAL)
    await service.admin_edit(started.id, "text editat")
    version = await service.approve(started.id, ReviewAction.APPROVE_LOCAL)
    assert version is not None and version.answer == "text editat"


async def test_reject_leaves_knowledge_untouched() -> None:
    """Rejecting changes no Q&A."""
    service, answers, items, _, _ = await _service()
    await _seed_answer(answers)
    started = await service.start("ans:m1", PRINCIPAL)
    assert started is not None
    await service.propose(started.id, "proposta", PRINCIPAL)
    rejected = await service.reject(started.id)
    assert rejected is not None and rejected.status is FeedbackStatus.REJECTED
    assert (
        await items.get_by_canonical_key(
            canonical_key_for("Com es demana l'equipament?")
        )
        is None
    )


async def test_global_and_local_variants_are_separate() -> None:
    """Local and global approvals create separate scoped items."""
    service, answers, items, versions, _ = await _service()
    first = await _seed_answer(answers)
    started = await service.start(first.id, PRINCIPAL)
    assert started is not None
    await service.propose(started.id, "Resposta local.", PRINCIPAL)
    local = await service.approve(started.id, ReviewAction.APPROVE_LOCAL)
    assert local is not None
    second = replace(first, id="ans:m2", user_message_id="m2")
    await answers.add(second)
    started_global = await service.start(second.id, PRINCIPAL)
    assert started_global is not None
    await service.propose(started_global.id, "Resposta global.", PRINCIPAL)
    global_version = await service.approve(
        started_global.id, ReviewAction.APPROVE_GLOBAL
    )
    assert global_version is not None
    local_item = await items.get(local.qa_id)
    global_item = await items.get(global_version.qa_id)
    assert local_item is not None and global_item is not None
    assert local_item.scope_key == GROUP_SCOPE_KEY
    assert global_item.scope_key == GLOBAL_SCOPE
    assert local_item.id != global_item.id
    assert await versions.get(local.id) is not None
