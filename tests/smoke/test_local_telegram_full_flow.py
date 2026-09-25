# SPDX-License-Identifier: MIT
"""Synthetic Telegram flows through the real local SQLite and Ollama graph."""

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest

from knowledge_bot.adapters.http.app import create_app
from knowledge_bot.domain.identity import canonical_key_for
from knowledge_bot.infrastructure.local.composition import build_context
from knowledge_bot.infrastructure.settings import RuntimeMode, Settings
from tests.fakes.support import FrozenClock, RecordingTransport

pytestmark = pytest.mark.e2e_local


def _message(
    text: str, message_id: int, chat_id: int, timestamp: int | None = None
) -> dict[str, object]:
    """Build one synthetic Telegram message update."""
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "date": timestamp or int(time.time()) + message_id,
            "chat": {"id": chat_id, "type": "supergroup"},
            "from": {"id": 111, "is_bot": False},
            "text": text,
        },
    }


def _callback(
    data: str, callback_id: int, from_id: int, chat_id: int
) -> dict[str, object]:
    """Build one synthetic Telegram callback update."""
    return {
        "update_id": callback_id,
        "callback_query": {
            "id": f"callback-{callback_id}",
            "from": {"id": from_id, "is_bot": False},
            "data": data,
            "message": {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "supergroup"},
            },
        },
    }


@pytest.mark.asyncio
async def test_synthetic_telegram_uses_real_local_graph(tmp_path: Path) -> None:
    """Exercise Telegram parsing, SQLite, NumPy, and Ollama without Telegram."""
    if os.getenv("RUN_LOCAL_AI_E2E") != "1":
        pytest.skip("set RUN_LOCAL_AI_E2E=1 to call the local runtime")
    path = str(tmp_path / "local.sqlite3")
    settings = Settings(
        _env_file=None,
        runtime=RuntimeMode.LOCAL,
        sqlite_path=path,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        embedding_model="embeddinggemma",
        generation_model="gemma3:270m",
        telegram_webhook_secret="local-secret",
        telegram_bot_id="999",
        telegram_bot_username="local_bot",
        admin_telegram_user_id="1",
        internal_admin_key="local-key",
    )
    transport = RecordingTransport()
    context, database, client = await build_context(settings, transport=transport)
    app = create_app(lambda request: context)
    headers = {
        "X-Telegram-Bot-Api-Secret-Token": "local-secret",
        "X-Internal-Key": "local-key",
    }
    marker = f"LOCAL-TELEGRAM-{uuid.uuid4().hex}"
    space_ids: dict[str, str] = {}
    # A realistic exchange: the model composes from the evidence, so a real
    # question is what exercises the whole graph.
    question = "A quina hora entrenen els minis del club?"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as api:
            for chat_id in (-100, -200):
                registered = await api.post(
                    "/internal/groups",
                    headers=headers,
                    json={"chat_id": str(chat_id), "title": f"Group {chat_id}"},
                )
                assert registered.status_code == 200, registered.text
                space_ids[str(chat_id)] = registered.json()["space_id"]
            seeded = await api.post(
                "/internal/seed",
                headers=headers,
                json={
                    "qa": [
                        {
                            "source_url": f"https://local.invalid/{marker}",
                            "source_kind": "local_e2e",
                            "source_authority": 90,
                            "section": "Local E2E",
                            "question": question,
                            "answer": (
                                "Els minis entrenen els dimarts a les sis del vespre."
                            ),
                            "status": "published",
                            "retrieved_at": "2026-09-24T00:00:00Z",
                        }
                    ]
                },
            )
            assert seeded.status_code == 200, seeded.text
            for chat_id in (-100, -200):
                response = await api.post(
                    "/telegram/webhook",
                    headers=headers,
                    json=_message(f"/ask {question}", 10 + chat_id, chat_id),
                )
                assert response.status_code == 200, response.text
                # The answer path is exercised end to end. The local 270m
                # model may decline, which is a correct outcome, so the
                # guarantee here is that a durable answer was delivered.
                assert transport.answers[-1][1]
            answer_id = transport.answers[0][2]
            nomination = _message("/reviewer", 15, -100)
            nomination_message = cast(dict[str, object], nomination["message"])
            nomination_message["from"] = {"id": 1, "is_bot": False}
            nomination_message["reply_to_message"] = {
                "message_id": 14,
                "date": int(time.time()),
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 222, "is_bot": False},
            }
            nominated = await api.post(
                "/telegram/webhook", headers=headers, json=nomination
            )
            assert nominated.status_code == 200, nominated.text
            assert nominated.json() == {"status": "reviewer_nominated"}
            started = await api.post(
                "/telegram/webhook",
                headers=headers,
                json=_callback(f"feedback:start:{answer_id}", 20, 222, -100),
            )
            assert started.status_code == 200, started.text
            assert started.json() == {"status": "feedback_started"}
            interaction = await context.interactions.get("1")
            assert interaction is not None
            proposal = _message(f"Correcció local: {marker}-LOCAL-CORRECTED", 21, 222)
            proposal_message = cast(dict[str, object], proposal["message"])
            proposal_message["from"] = {"id": 222, "is_bot": False}
            proposal_message["chat"] = {"id": 222, "type": "private"}
            proposal_message["reply_to_message"] = {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": 222, "type": "private"},
                "from": {"id": 999, "is_bot": True},
            }
            proposed = await api.post(
                "/telegram/webhook", headers=headers, json=proposal
            )
            assert proposed.status_code == 200, proposed.text
            assert proposed.json() == {"status": "proposed"}
            forged = await api.post(
                "/telegram/webhook",
                headers=headers,
                json=_callback(
                    f"feedback:approve-global:{interaction.object_id}",
                    22,
                    222,
                    -100,
                ),
            )
            assert forged.status_code == 200, forged.text
            assert forged.json() == {"status": "ignored"}
            approved = await api.post(
                "/telegram/webhook",
                headers=headers,
                json=_callback(
                    f"feedback:approve-group:{interaction.object_id}", 23, 1, -100
                ),
            )
            assert approved.status_code == 200, approved.text
            assert approved.json() == {"status": "feedback_approved"}
            # Group -100 now has the approved local correction, group -200
            # keeps the global seed. The deterministic guarantee is that each
            # group gets a durable answer; which words the 270m model echoes
            # is not a wiring property, so the per-space version is asserted
            # directly below instead.
            for chat_id in (-100, -200):
                response = await api.post(
                    "/telegram/webhook",
                    headers=headers,
                    json=_message(f"/ask {question}", 30 + chat_id, chat_id),
                )
                assert response.status_code == 200, response.text
                assert transport.answers[-1][1]
            # The approved correction is a new version scoped to group -100,
            # and group -200 still resolves the global seed: scope isolation
            # holds independently of what the small local model echoes.
            corrected_item = await context.feedback.qa_items.get_by_canonical_key(
                canonical_key_for(question), f"space:{space_ids['-100']}"
            )
            assert corrected_item is not None
            corrected_version = await context.feedback.qa_versions.get(
                corrected_item.current_version_id or ""
            )
            assert corrected_version is not None
            assert f"{marker}-LOCAL-CORRECTED" in corrected_version.answer
            global_item = await context.feedback.qa_items.get_by_canonical_key(
                canonical_key_for(question)
            )
            assert global_item is not None
            global_version = await context.feedback.qa_versions.get(
                global_item.current_version_id or ""
            )
            assert global_version is not None
            assert "LOCAL-CORRECTED" not in global_version.answer
    finally:
        await client.aclose()
        await database.close()


@pytest.mark.asyncio
async def test_background_pair_persists_and_retrieves_local_evidence(
    tmp_path: Path,
) -> None:
    """A quiet-period pair is persisted and visible only inside its source space."""
    if os.getenv("RUN_LOCAL_AI_E2E") != "1":
        pytest.skip("set RUN_LOCAL_AI_E2E=1 to call the local runtime")
    settings = Settings(
        _env_file=None,
        runtime=RuntimeMode.LOCAL,
        sqlite_path=str(tmp_path / "pair.sqlite3"),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        embedding_model="embeddinggemma",
        generation_model="gemma3:270m",
        telegram_webhook_secret="pair-secret",
        telegram_bot_id="999",
        telegram_bot_username="pair_bot",
        admin_telegram_user_id="1",
        internal_admin_key="pair-key",
        background_listener_enabled=True,
        pairing_question_window_minutes=1,
    )
    transport = RecordingTransport()
    base_time = datetime.now(UTC)
    clock = FrozenClock(base_time)
    context, database, client = await build_context(
        settings, transport=transport, clock=clock
    )
    app = create_app(lambda request: context)
    headers = {"X-Telegram-Bot-Api-Secret-Token": "pair-secret"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as api:
            for chat_id in (-100, -200):
                response = await api.post(
                    "/internal/groups",
                    headers={"X-Internal-Key": "pair-key"},
                    json={"chat_id": str(chat_id)},
                )
                assert response.status_code == 200, response.text
            texts = (
                "Question: What is the water temperature?",
                "Answer: The water temperature is exactly twenty degrees Celsius.",
                "Acknowledgment: Thanks, I will remember that.",
            )
            for index, text in enumerate(texts, start=1):
                clock.advance_to(
                    base_time + timedelta(seconds=30 * index if index < 3 else 180)
                )
                response = await api.post(
                    "/telegram/webhook",
                    headers=headers,
                    json=_message(text, index, -100, int(clock.now().timestamp())),
                )
                assert response.status_code == 200, response.text
            answer = await context.ingestor.messages.get("-100:2")
            assert answer is not None
            assert answer.context_question == texts[0]
            assert answer.index_status.value == "indexed"
            conversation_b = await context.ingestor.conversations.get("-200")
            assert conversation_b is not None and conversation_b.space_id is not None
            retrieved_b = await context.answer.retrieval.retrieve(
                texts[0], conversation_b.space_id
            )
            assert "-100:2" not in [item.source_id for item in retrieved_b.messages]
    finally:
        await client.aclose()
        await database.close()
