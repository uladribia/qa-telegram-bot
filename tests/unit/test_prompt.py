# SPDX-License-Identifier: MIT
"""Unit tests for the shared grounded-answer prompt."""

from knowledge_bot.infrastructure.prompt import SYSTEM_PROMPT, render_user
from knowledge_bot.ports.generator import EvidenceItem, GenerationRequest


def _request(*similarities: float) -> GenerationRequest:
    """Build a request whose evidence carries the given similarities."""
    return GenerationRequest(
        question="Quan entrenen?",
        evidence=[
            EvidenceItem(
                source_id=f"qa:qa-{index}",
                text=f"text {index}",
                label="Q&A",
                authority=90,
                similarity=similarity,
            )
            for index, similarity in enumerate(similarities, start=1)
        ],
    )


def test_every_evidence_line_carries_its_similarity() -> None:
    """The model sees how strongly each candidate matched the question."""
    rendered = render_user(_request(1.0, 0.4836))
    assert "[qa:qa-1] (Q&A, authority=90, similarity=1.00) text 1" in rendered
    assert "[qa:qa-2] (Q&A, authority=90, similarity=0.48) text 2" in rendered


def test_the_score_is_explained_but_is_not_a_gate() -> None:
    """The rules tell the model what a score means and when to ignore it."""
    assert "similarity score from 0 to 1" in SYSTEM_PROMPT
    assert "however high the score" in SYSTEM_PROMPT


def test_no_evidence_renders_the_placeholder() -> None:
    """An empty evidence set renders the placeholder, not an empty block."""
    assert render_user(GenerationRequest(question="q", evidence=[])).endswith(
        "EVIDENCE:\n(no evidence)"
    )
