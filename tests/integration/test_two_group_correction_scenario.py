# SPDX-License-Identifier: MIT
"""Two-group correction and retrieval scenario."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from knowledge_bot.adapters.telegram.routes import TELEGRAM_WEBHOOK_PATH
from knowledge_bot.api.app import create_app
from knowledge_bot.application.feedback import PROPOSAL_PROMPT
from knowledge_bot.application.reindex import ReindexService
from knowledge_bot.domain.enums import ReviewAction
from knowledge_bot.domain.identity import canonical_key_for
from knowledge_bot.domain.scope import scope_for_space
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.models.questions import AskQuestionRequest
from knowledge_bot.models.seed import SeedQA
from knowledge_bot.ports.generator import GenerationOutput, GenerationRequest
from knowledge_bot.ports.index import IndexableQA
from tests.fakes.ai import FakeSearchIndexSource
from tests.fakes.backend import InMemoryBackend
from tests.fakes.context import SPACE_A, SPACE_B, WEBHOOK_SECRET, build_test_context
from tests.fakes.search_index import RepositorySearchIndexSource
from tests.fakes.support import RecordingTransport

QUESTION = "Quina és la resposta local del grup?"
SCOPE_QUESTION = "Com es demana l'equipament?"
SECRET_HEADER = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
ADMIN_ID = 1
LOCAL_REVIEWER_ID = 222
REPORTER_ID = 555


class _EchoingGenerator:
    """Answer with the text of the first supplied evidence.

    The model produces the answer, so echoing the evidence keeps this
    scenario's assertion meaningful: the answer still shows which space's
    Q&A was retrieved.
    """

    def __init__(self) -> None:
        """Create the echoing generator."""
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Answer with the first evidence item's text."""
        self.requests.append(request)
        if not request.evidence:
            return GenerationOutput(status="insufficient", source_ids=[])
        first = request.evidence[0]
        return GenerationOutput(
            status="answered", answer=first.text, source_ids=[first.source_id]
        )


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
    context = replace(
        context, answer=replace(context.answer, generator=_EchoingGenerator())
    )

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


def _webhook_context() -> tuple[AppContext, RecordingTransport]:
    """Build a context whose index reads back the Q&A the app stored.

    The correction path projects the version an approval just wrote, so the
    index source has to resolve it from the repositories the way production
    resolves it from D1. A hand-fed source cannot: the id does not exist yet.
    """
    backend = InMemoryBackend()
    context, transport = build_test_context(backend=backend)
    source = RepositorySearchIndexSource(backend.qa_items, backend.qa_versions)
    projector = replace(context.projector, source=source)
    return (
        replace(
            context,
            projector=projector,
            reindex=ReindexService(source, projector, context.clock),
            answer=replace(context.answer, generator=_EchoingGenerator()),
        ),
        transport,
    )


def _ask(client: TestClient, chat_id: int, message_id: int) -> None:
    """Ask the bot in a group; the delivered answer is on the transport."""
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        headers=SECRET_HEADER,
        json={
            "update_id": message_id,
            "message": {
                "message_id": message_id,
                "date": 1789000000,
                "chat": {"id": chat_id, "type": "supergroup"},
                "from": {"id": 10 + chat_id, "is_bot": False},
                "text": f"/ask {SCOPE_QUESTION}",
            },
        },
    )
    assert response.json() == {"status": "answer"}


def _nominate_local_reviewer(client: TestClient) -> None:
    """Make the local reviewer of group -100 the person pressing the buttons."""
    response = client.post(
        TELEGRAM_WEBHOOK_PATH,
        headers=SECRET_HEADER,
        json={
            "update_id": 90,
            "message": {
                "message_id": 90,
                "date": 1789000000,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": ADMIN_ID, "is_bot": False},
                "text": "/reviewer",
                "reply_to_message": {
                    "message_id": 91,
                    "date": 1789000000,
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {
                        "id": LOCAL_REVIEWER_ID,
                        "is_bot": False,
                        "first_name": "Pepe",
                    },
                },
            },
        },
    )
    assert response.json() == {"status": "reviewer_nominated"}


def _correct(
    client: TestClient,
    transport: RecordingTransport,
    *,
    answer_id: str,
    chat_id: int,
    update_id: int,
    proposal: str,
    approver_id: int,
    scope: str,
) -> None:
    """Flag, propose, and approve an answer over the webhook."""
    pressed = client.post(
        TELEGRAM_WEBHOOK_PATH,
        headers=SECRET_HEADER,
        json=_callback(f"feedback:start:{answer_id}", update_id, REPORTER_ID, chat_id),
    )
    assert pressed.json() == {"status": "feedback_started"}
    prompt_id = len(transport.force_replies)
    proposed = client.post(
        TELEGRAM_WEBHOOK_PATH,
        headers=SECRET_HEADER,
        json=_proposal_reply(update_id + 1, prompt_id, REPORTER_ID, proposal),
    )
    assert proposed.json() == {"status": "proposed"}
    approved = client.post(
        TELEGRAM_WEBHOOK_PATH,
        headers=SECRET_HEADER,
        json=_callback(
            f"feedback:{scope}:fb:{answer_id}", update_id + 2, approver_id, chat_id
        ),
    )
    assert approved.json() == {"status": "feedback_approved"}


def _callback(
    data: str, update_id: int, from_id: int, chat_id: int
) -> dict[str, object]:
    """Build a callback query press in a group."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": from_id, "is_bot": False, "first_name": "Marta"},
            "data": data,
            "message": {
                "message_id": 42,
                "date": 1789000000,
                "chat": {"id": chat_id, "type": "supergroup"},
                "from": {"id": 999, "is_bot": True},
                "text": "resposta",
            },
        },
    }


def _proposal_reply(
    update_id: int, prompt_id: int, from_id: int, text: str
) -> dict[str, object]:
    """Build the reporter's private reply to the proposal prompt."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1789000000,
            "chat": {"id": from_id, "type": "private"},
            "from": {"id": from_id, "is_bot": False},
            "text": text,
            "reply_to_message": {
                "message_id": prompt_id,
                "date": 1789000000,
                "chat": {"id": from_id, "type": "private"},
                "from": {"id": 999, "is_bot": True},
                "text": PROPOSAL_PROMPT,
            },
        },
    }


def test_global_correction_leaves_the_local_variant_answering() -> None:
    """A local correction survives a later global one, in its own group only.

    This is the two-group acceptance from ``docs/e2e-telegram.md`` steps 8 and
    9, run offline: A gets a local correction, a global correction then lands,
    and each group must still answer from its own scope.
    """
    context, transport = _webhook_context()
    client = TestClient(create_app(lambda request: context))

    async def seed_global() -> None:
        created, _, _, _, versions = await context.seed.seed_qa(
            [
                SeedQA(
                    source_url="https://local.invalid/equipament",
                    source_authority=90,
                    section="Equipament",
                    question=SCOPE_QUESTION,
                    answer="Global V1",
                    status="published",
                    retrieved_at=datetime(2026, 9, 24, tzinfo=UTC),
                    source_anchor="qa-equipament",
                )
            ]
        )
        assert created == 1
        assert await context.reindex.reindex_qa_version(versions[0])

    asyncio.run(seed_global())

    _ask(client, -100, 11)
    _ask(client, -200, 12)
    assert transport.answers[-2][1].startswith("Global V1")
    assert transport.answers[-1][1].startswith("Global V1")

    _nominate_local_reviewer(client)
    _correct(
        client,
        transport,
        answer_id="ans:-100:11",
        chat_id=-100,
        update_id=30,
        proposal="Resposta local A.",
        approver_id=LOCAL_REVIEWER_ID,
        scope="approve-group",
    )
    # The group's own reviewer decided it; the admin was never asked.
    assert [chat for chat, _, _ in transport.reviews] == [str(LOCAL_REVIEWER_ID)]
    _ask(client, -100, 13)
    _ask(client, -200, 14)
    assert transport.answers[-2][1].startswith("Resposta local A.")
    assert transport.answers[-1][1].startswith("Global V1")

    _correct(
        client,
        transport,
        answer_id="ans:-200:14",
        chat_id=-200,
        update_id=40,
        proposal="Global V2",
        approver_id=ADMIN_ID,
        scope="approve-global",
    )
    # Group B has no local reviewer, so its correction went to the admin.
    assert [chat for chat, _, _ in transport.reviews] == [
        str(LOCAL_REVIEWER_ID),
        "1",
    ]
    _ask(client, -100, 15)
    _ask(client, -200, 16)
    assert transport.answers[-2][1].startswith("Resposta local A.")
    assert transport.answers[-1][1].startswith("Global V2")

    local_item = asyncio.run(
        context.feedback.qa_items.get_by_canonical_key(
            canonical_key_for(SCOPE_QUESTION), scope_for_space(SPACE_A)
        )
    )
    global_item = asyncio.run(
        context.feedback.qa_items.get_by_canonical_key(
            canonical_key_for(SCOPE_QUESTION)
        )
    )
    assert local_item is not None and global_item is not None
    assert local_item.id != global_item.id
