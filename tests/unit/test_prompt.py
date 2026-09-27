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
    assert (
        "[qa:qa-1] (official club knowledge, authority=90, similarity=1.00)" in rendered
    )
    assert (
        "[qa:qa-2] (official club knowledge, authority=90, similarity=0.48)" in rendered
    )


def test_an_item_renders_the_question_it_answers() -> None:
    """The model is told what each piece of evidence is an answer to."""
    request = GenerationRequest(
        question="com es fan els pagaments?",
        evidence=[
            EvidenceItem(
                source_id="qa:qa-1",
                text="Els pagaments es fan a través de Cluber.",
                label="Q&A",
                authority=90,
                similarity=0.6,
                provenance="official",
                question="Com es paguen la quota i la llicència federativa?",
            )
        ],
    )
    rendered = render_user(request)
    assert "  Q: Com es paguen la quota i la llicència federativa?" in rendered
    assert "  A: Els pagaments es fan a través de Cluber." in rendered


def test_every_answer_line_is_prefixed() -> None:
    """A multi-line answer cannot bleed into the next evidence item."""
    request = GenerationRequest(
        question="quant costa?",
        evidence=[
            EvidenceItem(
                source_id="qa:qa-1",
                text="- Samarreta: 38,50€\n- Pantaló: 29,50€",
                label="Q&A",
                authority=90,
                similarity=0.6,
            ),
            EvidenceItem(
                source_id="qa:qa-2",
                text="Altra cosa",
                label="Q&A",
                authority=90,
                similarity=0.5,
            ),
        ],
    )
    rendered = render_user(request)
    assert "  A: - Samarreta: 38,50€\n  A: - Pantaló: 29,50€" in rendered
    assert "\n- Pantaló" not in rendered


def test_the_rules_tie_an_item_to_its_question() -> None:
    """The model answers the user's question, not the evidence's question."""
    assert "prefixed Q:" in SYSTEM_PROMPT
    assert "Answer the user's question" in SYSTEM_PROMPT
    assert "does not answer it" in SYSTEM_PROMPT
    assert "does not answer how to pay it" in SYSTEM_PROMPT


def test_reported_evidence_is_labelled_as_unverified() -> None:
    """Group evidence is marked as weaker and carries its source type."""
    request = GenerationRequest(
        question="Com es paguen?",
        evidence=[
            EvidenceItem(
                source_id="msg:abc",
                text="veig això?",
                label="Grup",
                authority=40,
                similarity=0.52,
                provenance="reported",
                source_kind="whatsapp",
            )
        ],
    )
    rendered = render_user(request)
    assert (
        "[msg:abc] (unverified, inferred from group chat, source=whatsapp,"
        " authority=40, similarity=0.52)" in rendered
    )
    assert "  A: veig això?" in rendered


def test_the_evidence_line_carries_facts_not_explanation() -> None:
    """Each line states the item's own provenance, nothing more.

    How reported evidence is gathered is explained once in the system prompt,
    so it must not be restated on every evidence line.
    """
    request = GenerationRequest(
        question="On és el camp?",
        evidence=[
            EvidenceItem(
                source_id="msg:abc",
                text="Estan tots a dins",
                label="Grup",
                authority=40,
                similarity=0.5,
                provenance="reported",
                source_kind="whatsapp",
            )
        ],
    )
    rendered = render_user(request)
    assert "stored against" not in rendered
    assert "classifier" not in rendered
    assert "replying to" not in rendered


def test_the_rules_explain_how_reported_evidence_is_gathered() -> None:
    """The gathering mechanism is explained once, in the system prompt."""
    assert "the question its message replied to" in SYSTEM_PROMPT
    assert "whatever the group happened to be discussing" in SYSTEM_PROMPT


def test_official_evidence_outranks_reported_evidence() -> None:
    """The rules state the trust ordering between the two provenance tiers."""
    assert "official club knowledge" in SYSTEM_PROMPT
    assert "more trustworthy" in SYSTEM_PROMPT
    assert "never treat it as equal to an official source" in SYSTEM_PROMPT


def test_provenance_never_licenses_an_unrelated_answer() -> None:
    """Trust says where a fact came from, not that it answers the question."""
    assert "however high the score and however official" in SYSTEM_PROMPT
    assert "it never replaces the" in SYSTEM_PROMPT
    assert "requirement that the text answer the question" in SYSTEM_PROMPT


def test_the_score_is_explained_but_is_not_a_gate() -> None:
    """The rules tell the model what a score means and when to ignore it."""
    assert "similarity score from 0 to 1" in SYSTEM_PROMPT
    assert "however high the score" in SYSTEM_PROMPT


def test_no_evidence_renders_the_placeholder() -> None:
    """An empty evidence set renders the placeholder, not an empty block."""
    assert render_user(GenerationRequest(question="q", evidence=[])).endswith(
        "EVIDENCE:\n(no evidence)"
    )
