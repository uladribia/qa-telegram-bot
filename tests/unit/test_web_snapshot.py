# SPDX-License-Identifier: MIT
"""Tests for the web Q&A snapshot parser (synthetic HTML, no real data)."""

from datetime import UTC, datetime

from knowledge_bot.adapters.inbound.web_snapshot import html_to_text, parse_qa_html

RETRIEVED = datetime(2026, 9, 19, tzinfo=UTC)

HTML = """
<html><head><style>.qa-item { color: red; }</style></head><body>
<div class="topic" id="topic-a">
  <div class="topic-head"><h3>Equipament</h3></div>
  <div class="topic-items">
    <details class="qa-item" id="qa-order">
      <summary>
        <span class="qa-num">01</span>
        <span class="qa-question">Com es demana l'equipament?</span>
      </summary>
      <div class="qa-answer">
        <p>Els nens es proven les <strong>talles</strong>.</p>
        <ol><li>El delegat envia una llista.</li><li>La familia comanda.</li></ol>
      </div>
    </details>
    <details class="qa-item" id="qa-arrival">
      <summary>
        <span class="qa-question">Quan arriben els equipaments?</span>
        <span class="badge badge--pending">En revisió</span>
      </summary>
      <div class="qa-answer"><p>Encara no està confirmat.</p></div>
    </details>
  </div>
</div>
<div class="topic" id="topic-b">
  <div class="topic-head"><h3>Salut</h3></div>
  <div class="topic-items">
    <details class="qa-item" id="qa-medical">
      <summary><span class="qa-question">Cal certificat mèdic?</span></summary>
      <div class="qa-answer"><p>Sí, i cal renovar-lo.</p></div>
    </details>
  </div>
</div>
</body></html>
"""


def test_parses_questions_answers_and_sections() -> None:
    """Each Q&A is extracted with its section and anchor."""
    entries = parse_qa_html(
        HTML, source_url="https://example.com/", retrieved_at=RETRIEVED
    )
    assert len(entries) == 3
    first = entries[0]
    assert first.section == "Equipament"
    assert first.source_anchor == "qa-order"
    assert first.question == "Com es demana l'equipament?"
    assert first.status == "published"
    assert "talles" in first.answer
    assert "El delegat envia una llista." in first.answer
    assert entries[2].section == "Salut"


def test_pending_entries_are_marked_in_review() -> None:
    """The pending badge marks the entry as in review."""
    entries = parse_qa_html(
        HTML, source_url="https://example.com/", retrieved_at=RETRIEVED
    )
    pending = next(entry for entry in entries if entry.source_anchor == "qa-arrival")
    assert pending.status == "in_review"


def test_html_to_text_normalises_markup() -> None:
    """Tags are stripped and lists become lines."""
    text = html_to_text(
        "<p>Hola <strong>món</strong>.</p><ul><li>Un</li><li>Dos</li></ul>"
    )
    assert "Hola món." in text
    assert "- Un" in text
    assert "- Dos" in text
    assert "<" not in text


def test_parser_accepts_configurable_class_names() -> None:
    """The parser is not tied to one site's class names."""
    from knowledge_bot.adapters.inbound.web_snapshot import SnapshotConfig

    html = (
        '<div class="faq-group">'
        "<h2>Secció</h2>"
        '<details class="qa-item"><span class="qa-question">P?</span>'
        '<div class="qa-answer">R.</div></details>'
        "</div>"
    )
    config = SnapshotConfig(topic_class="faq-group", topic_title_tag="h2")
    entries = parse_qa_html(
        html, source_url="https://example.com/", retrieved_at=RETRIEVED, config=config
    )
    assert len(entries) == 1
    assert entries[0].section == "Secció"
    assert entries[0].question == "P?"
    assert entries[0].answer == "R."
