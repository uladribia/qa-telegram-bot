# SPDX-License-Identifier: MIT
"""Web Q&A snapshot parser (spec §7).

The parser is structure-driven and configurable so it is not tied to one site:
the caller supplies the CSS class names to look for. It converts the answer
markup to plain text and preserves the section, the real anchor, and the
"pending"/in-review status.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from urllib.request import Request, urlopen

from knowledge_bot.contracts.seed import SeedQA

_UA = "knowledge-bot-snapshot/1.0"
_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK_END = re.compile(r"</(p|li|ol|ul|div|h[1-6]|tr)>", re.IGNORECASE)
_LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LIST_ITEM = re.compile(r"<li[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def _class_attr(token: str) -> str:
    """Build a regex matching an exact class token inside a class attribute."""
    return rf'class="[^"]*(?<![-\w]){re.escape(token)}(?![-\w])[^"]*"'


@dataclass(frozen=True, slots=True)
class SnapshotConfig:
    """CSS class names that describe the site structure."""

    topic_class: str = "topic"
    topic_title_tag: str = "h3"
    item_class: str = "qa-item"
    question_class: str = "qa-question"
    answer_class: str = "qa-answer"
    pending_marker: str = "badge--pending"


def html_to_text(fragment: str) -> str:
    """Convert a small HTML fragment to plain text.

    Args:
        fragment: The HTML fragment.

    Returns:
        Plain text with list items and paragraphs on separate lines.
    """
    text = _LINE_BREAK.sub("\n", fragment)
    text = _LIST_ITEM.sub("- ", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = unescape(text)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line == "" and (not lines or lines[-1] == ""):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def parse_qa_html(
    html: str,
    *,
    source_url: str,
    retrieved_at: datetime | None = None,
    config: SnapshotConfig | None = None,
) -> list[SeedQA]:
    """Extract Q&A entries from a snapshot HTML page.

    Args:
        html: The full HTML document.
        source_url: The URL the snapshot came from.
        retrieved_at: When the snapshot was taken; defaults to now (UTC).
        config: The CSS class names describing the page structure.

    Returns:
        The parsed Q&A entries in document order.
    """
    resolved = config if config is not None else SnapshotConfig()
    taken_at = retrieved_at if retrieved_at is not None else datetime.now(UTC)
    body = _SCRIPT_STYLE.sub("", html)
    topic_pattern = re.compile(
        rf"<div[^>]*{_class_attr(resolved.topic_class)}[^>]*>",
        re.IGNORECASE,
    )
    starts = [match.start() for match in topic_pattern.finditer(body)]
    entries: list[SeedQA] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(body)
        entries.extend(
            _parse_topic(
                body[start:end],
                source_url=source_url,
                retrieved_at=taken_at,
                config=resolved,
            )
        )
    return entries


def _parse_topic(
    chunk: str,
    *,
    source_url: str,
    retrieved_at: datetime,
    config: SnapshotConfig,
) -> list[SeedQA]:
    title = re.search(
        rf"<{config.topic_title_tag}[^>]*>(.*?)</{config.topic_title_tag}>",
        chunk,
        re.IGNORECASE | re.DOTALL,
    )
    section = html_to_text(title.group(1)) if title is not None else ""
    item_pattern = re.compile(
        rf"<details[^>]*{_class_attr(config.item_class)}[^>]*>(.*?)</details>",
        re.IGNORECASE | re.DOTALL,
    )
    entries: list[SeedQA] = []
    for item in item_pattern.finditer(chunk):
        block = item.group(1)
        question = re.search(
            rf"<span[^>]*{_class_attr(config.question_class)}[^>]*>(.*?)</span>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        answer = re.search(
            rf"<div[^>]*{_class_attr(config.answer_class)}[^>]*>(.*?)</div>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if question is None or answer is None:
            continue
        anchor = re.search(r'\bid="([^"]+)"', item.group(0), re.IGNORECASE)
        in_review = config.pending_marker in item.group(0)
        entries.append(
            SeedQA(
                source_url=source_url,
                source_anchor=anchor.group(1) if anchor is not None else None,
                section=section,
                question=html_to_text(question.group(1)),
                answer=html_to_text(answer.group(1)),
                status="in_review" if in_review else "published",
                retrieved_at=retrieved_at,
            )
        )
    return entries


def fetch_html(url: str, *, timeout: float = 30.0) -> str:
    """Fetch a URL and return its HTML.

    Args:
        url: The page to fetch.
        timeout: Request timeout in seconds.

    Returns:
        The response body as text.
    """
    request = Request(url, headers={"User-Agent": _UA})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")
