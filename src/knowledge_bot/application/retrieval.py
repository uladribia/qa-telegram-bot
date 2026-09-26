# SPDX-License-Identifier: MIT
"""Retrieval: one embedding per question, one cosine threshold.

The question is embedded once and the vector index returns its nearest Q&A and
group-evidence candidates. A single cosine threshold decides what evidence
exists at all, and the nearest few above it are what the model is given. Local
Q&A variants suppress global ones for the same canonical question.

There is deliberately no lexical (BM25/FTS5) leg and no cross-encoder. As
written the lexical leg fired for 2 of 91 eval questions, and a fixed
OR-of-tokens query was worth +2 questions in 43 at Recall@5; the cross-encoder
thinned the prompt enough to make the bot worse. See
[docs/experiments.md](docs/experiments.md).
"""

import re
from dataclasses import dataclass, field

from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.vector_store import VectorMatch, VectorStore

QA_KIND = "qa"
MESSAGE_KIND = "message_evidence"

_CANDIDATE_POOL = 15
_TOKEN = re.compile(r"\w+", flags=re.UNICODE)


@dataclass(frozen=True, slots=True)
class Evidence:
    """A retrieved piece of evidence about a question."""

    source_id: str
    label: str
    text: str
    authority: int
    similarity: float
    question: str | None = None
    qa_item_id: str | None = None
    qa_version_id: str | None = None
    anchor: str | None = None
    url: str | None = None
    date: str | None = None
    author: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievedEvidence:
    """Q&A and message evidence for a question."""

    qa: list[Evidence] = field(default_factory=list)
    messages: list[Evidence] = field(default_factory=list)

    def all(self) -> list[Evidence]:
        """Return all evidence."""
        return [*self.qa, *self.messages]


def _rank(matches: list[VectorMatch], top_k: int) -> list[VectorMatch]:
    """Order candidates by cosine similarity, then authority, best first."""
    ranked = sorted(
        matches,
        key=lambda match: (
            match.score,
            _as_int(match.metadata.get("authority")),
        ),
        reverse=True,
    )
    return ranked[:top_k]


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _opt_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _question_key(match: VectorMatch) -> str | None:
    """Return the canonical question key of a Q&A match, if any."""
    key = match.metadata.get("canonical_key")
    return key if isinstance(key, str) and key else None


def _to_evidence(match: VectorMatch, kind: str) -> Evidence:
    metadata = match.metadata
    question = metadata.get("question")
    return Evidence(
        source_id=match.id,
        label="Q&A" if kind == QA_KIND else "Grup",
        text=str(metadata.get("text", "")),
        authority=_as_int(metadata.get("authority")),
        similarity=match.score,
        question=question if isinstance(question, str) else None,
        qa_item_id=_opt_text(metadata.get("object_id")),
        qa_version_id=_opt_text(metadata.get("version_id")),
        anchor=_opt_text(metadata.get("source_anchor"))
        or _opt_text(metadata.get("canonical_key")),
        url=_opt_text(metadata.get("url")),
        date=_opt_text(metadata.get("date")),
        author=_opt_text(metadata.get("author")),
    )


@dataclass(frozen=True, slots=True)
class RetrievalService:
    """Retrieve evidence from the vector projection by cosine similarity."""

    embedder: Embedder
    vectors: VectorStore
    qa_top_k: int = 3
    message_top_k: int = 2

    async def retrieve(
        self,
        question: str,
        space_id: str | None = None,
        *,
        all_scopes: bool = False,
    ) -> RetrievedEvidence:
        """Return Q&A and message evidence for a question.

        Evidence is scoped: Q&A matches come from the global layer plus the
        asking space's own knowledge; messages come only from that space.
        Without a space id, normal retrieval is global-only.

        Args:
            question: The user question.
            space_id: The resolved logical space, when known.
            all_scopes: Internal evaluation mode; user paths never enable it.

        Returns:
            The retrieved evidence, strongest first.
        """
        embeddings = await self.embedder.embed([question])
        vector = embeddings[0] if embeddings else []
        base_filters: dict[str, object] = {"kind": QA_KIND, "status": "active"}
        if all_scopes:
            qa_lists: list[list[VectorMatch]] = [
                await self.vectors.query(
                    vector, top_k=_CANDIDATE_POOL, filters=base_filters
                )
            ]
        else:
            qa_lists = [
                await self.vectors.query(
                    vector,
                    top_k=_CANDIDATE_POOL,
                    filters={**base_filters, "scope_key": GLOBAL_SCOPE},
                )
            ]
            if space_id is not None:
                local_scope = scope_for_space(space_id)
                qa_lists.append(
                    await self.vectors.query(
                        vector,
                        top_k=_CANDIDATE_POOL,
                        filters={**base_filters, "scope_key": local_scope},
                    )
                )
        qa_matches = _select_qa(qa_lists, self.qa_top_k)
        message_filters: dict[str, object] = {"kind": MESSAGE_KIND}
        if space_id is not None:
            message_scope: str | None = scope_for_space(space_id)
        elif all_scopes:
            message_scope = None
        else:
            message_scope = GLOBAL_SCOPE
        if message_scope is not None:
            message_filters["scope_key"] = message_scope
        message_matches = _rank(
            await self.vectors.query(
                vector, top_k=_CANDIDATE_POOL, filters=message_filters
            ),
            self.message_top_k,
        )
        return RetrievedEvidence(
            qa=[_to_evidence(match, QA_KIND) for match in qa_matches],
            messages=[_to_evidence(match, MESSAGE_KIND) for match in message_matches],
        )


def _select_qa(lists: list[list[VectorMatch]], top_k: int) -> list[VectorMatch]:
    """Return the best Q&A candidates, dropping global ones overridden locally.

    List 0 is the global semantic list; lists 1+ are the asking space's local
    list. A local candidate suppresses the global candidate of the same
    canonical question before ranking.
    """
    local_keys = {_question_key(match) for ranked in lists[1:] for match in ranked} - {
        None
    }
    kept: list[VectorMatch] = []
    seen: set[str] = set()
    for index, ranked in enumerate(lists):
        for match in ranked:
            if index == 0 and local_keys and _question_key(match) in local_keys:
                continue
            if match.id in seen:
                continue
            seen.add(match.id)
            kept.append(match)
    return _rank(kept, top_k)
