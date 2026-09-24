# SPDX-License-Identifier: MIT
"""Hybrid retrieval: semantic vectors + BM25/FTS5 fused with RRF.

One embedding call per user question. Semantic and lexical candidate lists are
combined with Reciprocal Rank Fusion (never by mixing raw scores), local
Q&A variants suppress global ones for the same canonical question, and
authority breaks remaining ties before the semantic similarity does.
"""

import re
from dataclasses import dataclass, field

from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.lexical import LexicalIndex
from knowledge_bot.ports.vector_store import VectorMatch, VectorStore

QA_KIND = "qa"
MESSAGE_KIND = "message_evidence"

_RRF_K = 60
_CANDIDATE_POOL = 10
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


@dataclass(frozen=True, slots=True)
class _Fused:
    """One hybrid candidate ranked by RRF, authority, then similarity."""

    id: str
    rrf: float
    similarity: float
    authority: int
    metadata: dict[str, object]


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


def _to_evidence(match: _Fused, kind: str) -> Evidence:
    metadata = match.metadata
    question = metadata.get("question")
    return Evidence(
        source_id=match.id,
        label="Q&A" if kind == QA_KIND else "Grup",
        text=str(metadata.get("text", "")),
        authority=match.authority,
        similarity=match.similarity,
        question=question if isinstance(question, str) else None,
        qa_item_id=_opt_text(metadata.get("object_id")),
        qa_version_id=_opt_text(metadata.get("version_id")),
        anchor=_opt_text(metadata.get("source_anchor"))
        or _opt_text(metadata.get("canonical_key")),
        url=_opt_text(metadata.get("url")),
        date=_opt_text(metadata.get("date")),
        author=_opt_text(metadata.get("author")),
    )


def _rrf_fuse(ranked_lists: list[list[VectorMatch]]) -> dict[str, _Fused]:
    """Fuse ranked candidate lists with Reciprocal Rank Fusion.

    Each list contributes ``1 / (60 + rank)`` per candidate. A semantic list
    also contributes its cosine similarity as the final tie-break; lexical
    matches contribute rank only. The first occurrence of an id carries its
    metadata.
    """
    fused: dict[str, _Fused] = {}
    for ranked in ranked_lists:
        for rank, match in enumerate(ranked, start=1):
            contribution = 1.0 / (_RRF_K + rank)
            existing = fused.get(match.id)
            if existing is None:
                fused[match.id] = _Fused(
                    id=match.id,
                    rrf=contribution,
                    similarity=match.score,
                    authority=_as_int(match.metadata.get("authority")),
                    metadata=match.metadata,
                )
            else:
                fused[match.id] = _Fused(
                    id=match.id,
                    rrf=existing.rrf + contribution,
                    similarity=max(existing.similarity, match.score),
                    authority=existing.authority,
                    metadata=existing.metadata,
                )
    return fused


def _rank(fused: dict[str, _Fused], top_k: int) -> list[_Fused]:
    """Order candidates by RRF, authority, then semantic similarity."""
    ranked = sorted(
        fused.values(),
        key=lambda candidate: (
            candidate.rrf,
            candidate.authority,
            candidate.similarity,
        ),
        reverse=True,
    )
    return ranked[:top_k]


async def _lexical_as_semantic(
    index: LexicalIndex,
    query: str,
    kind: str,
    scope_key: str | None,
) -> list[VectorMatch]:
    """Run one BM25 search and normalize it to vector-match shape."""
    filters: dict[str, object] = {"kind": kind}
    if scope_key is not None:
        filters["scope_key"] = scope_key
    tokens = " ".join(f'"{token}"' for token in _TOKEN.findall(query))
    if not tokens:
        return []
    matches = await index.search(tokens, top_k=_CANDIDATE_POOL, filters=filters)
    return [
        VectorMatch(id=item.id, score=0.0, metadata=dict(item.metadata))
        for item in matches
    ]


@dataclass(frozen=True, slots=True)
class RetrievalService:
    """Retrieve evidence from the hybrid vector + lexical projections."""

    embedder: Embedder
    vectors: VectorStore
    lexical: LexicalIndex
    qa_top_k: int = 5
    message_top_k: int = 4

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
                ),
                await _lexical_as_semantic(self.lexical, question, QA_KIND, None),
            ]
        else:
            qa_lists = [
                await self.vectors.query(
                    vector,
                    top_k=_CANDIDATE_POOL,
                    filters={**base_filters, "scope_key": GLOBAL_SCOPE},
                ),
                await _lexical_as_semantic(
                    self.lexical, question, QA_KIND, GLOBAL_SCOPE
                ),
            ]
            if space_id is not None:
                local_scope = scope_for_space(space_id)
                qa_lists += [
                    await self.vectors.query(
                        vector,
                        top_k=_CANDIDATE_POOL,
                        filters={**base_filters, "scope_key": local_scope},
                    ),
                    await _lexical_as_semantic(
                        self.lexical, question, QA_KIND, local_scope
                    ),
                ]
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
        message_lists = [
            await self.vectors.query(
                vector, top_k=_CANDIDATE_POOL, filters=message_filters
            ),
            await _lexical_as_semantic(
                self.lexical, question, MESSAGE_KIND, message_scope
            ),
        ]
        message_matches = _rank(_rrf_fuse(message_lists), self.message_top_k)
        return RetrievedEvidence(
            qa=[_to_evidence(match, QA_KIND) for match in qa_matches],
            messages=[_to_evidence(match, MESSAGE_KIND) for match in message_matches],
        )


def _select_qa(lists: list[list[VectorMatch]], top_k: int) -> list[_Fused]:
    """Fuse scope lists, dropping global candidates overridden locally.

    Lists 0-1 are the global semantic/lexical space; lists 2+ are the asking
    space's local semantic/lexical matches. A local candidate suppresses the
    global candidate of the same canonical question before fusion.
    """
    local_keys = {_question_key(match) for ranked in lists[2:] for match in ranked} - {
        None
    }
    filtered: list[list[VectorMatch]] = []
    for index, ranked in enumerate(lists):
        if index < 2 and local_keys:
            filtered.append(
                [match for match in ranked if _question_key(match) not in local_keys]
            )
        else:
            filtered.append(ranked)
    return _rank(_rrf_fuse(filtered), top_k)
