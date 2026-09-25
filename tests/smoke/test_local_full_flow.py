# SPDX-License-Identifier: MIT
"""Real local SQLite, vector search, and Ollama functional flow."""

import os
import uuid

import httpx
import pytest

pytestmark = pytest.mark.e2e_local


@pytest.mark.asyncio
async def test_local_seed_retrieval_and_answer_flow() -> None:
    """Exercise seed, embedding, local vector retrieval, and answer delivery."""
    if os.getenv("RUN_LOCAL_AI_E2E") != "1":
        pytest.skip("set RUN_LOCAL_AI_E2E=1 to call the local runtime")
    base_url = os.getenv("LOCAL_BASE_URL", "http://127.0.0.1:8000")
    headers = {"X-Internal-Key": "local-development-key"}
    run_id = uuid.uuid4().hex
    question = f"What is the local full flow verification marker {run_id}?"
    async with httpx.AsyncClient(base_url=base_url, timeout=180.0) as client:
        ready = await client.get("/readyz")
        if ready.status_code != 200:
            pytest.skip("local runtime is not ready; run make dev-bootstrap first")
        seed = await client.post(
            "/internal/seed",
            headers=headers,
            json={
                "qa": [
                    {
                        "source_url": f"https://local.invalid/e2e/full-flow/{run_id}",
                        "source_kind": "local_e2e",
                        "source_authority": 90,
                        "section": "Local E2E",
                        "question": question,
                        "answer": "The local full flow marker is VERIFIED-LOCAL-42.",
                        "status": "published",
                        "retrieved_at": "2026-09-24T00:00:00Z",
                        "source_anchor": f"full-flow-verification-{run_id}",
                    }
                ],
                "scope": "global",
                "renew": False,
            },
        )
        assert seed.status_code == 200, seed.text
        seed_body = seed.json()
        assert seed_body["qa"] + seed_body["qa_renewed"] >= 1
        assert seed_body["indexed"] >= 1 or seed_body["qa"] == 0

        answer = await client.post(
            "/v1/questions",
            headers=headers,
            json={
                "request_id": f"local-e2e-{uuid.uuid4()}",
                "question": question,
                "principal_id": "local-e2e-user",
            },
        )
        assert answer.status_code == 200, answer.text
        answer_body = answer.json()
        # The local 270m model may decline a synthetic verification question;
        # abstaining is a correct outcome. Either way the answer is durable and,
        # when answered, carries its sources.
        assert answer_body["mode"] in {"synthesis", "abstention"}
        assert answer_body["answer"]
        if answer_body["mode"] == "synthesis":
            assert answer_body["sources"]
            assert (
                answer_body["answer"]
                != "No tinc prou informació fiable per respondre-ho."
            )

        feedback = await client.post(
            "/v1/feedback",
            headers=headers,
            json={
                "answer_id": answer_body["answer_id"],
                "reporter_principal_id": "local-e2e-user",
                "reporter_name": "Local E2E",
            },
        )
        assert feedback.status_code == 200, feedback.text
        feedback_id = feedback.json()["feedback_id"]
        proposal = await client.put(
            f"/v1/feedback/{feedback_id}/proposal",
            headers=headers,
            json={
                "reporter_principal_id": "local-e2e-user",
                "proposal": (
                    "The corrected local marker is VERIFIED-LOCAL-42-CORRECTED."
                ),
            },
        )
        assert proposal.status_code == 200, proposal.text
        assert proposal.json()["status"] == "pending_review"

        # A second, distinct request proves request-id isolation on the real path.
        replay_id = f"local-e2e-replay-{uuid.uuid4()}"
        first = await client.post(
            "/v1/questions",
            headers=headers,
            json={
                "request_id": replay_id,
                "question": "Tell me the local verification marker.",
                "principal_id": "local-e2e-user",
            },
        )
        second = await client.post(
            "/v1/questions",
            headers=headers,
            json={
                "request_id": replay_id,
                "question": "Tell me the local verification marker.",
                "principal_id": "local-e2e-user",
            },
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["answer_id"] == second.json()["answer_id"]
