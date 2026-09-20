# SPDX-License-Identifier: MIT
"""Retrieval: embed the question and fetch Q&A and message evidence."""

from dataclasses import dataclass, field

from knowledge_bot.domain.scope import GLOBAL_SCOPE
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.vector_store import VectorMatch, VectorStore

QA_KIND = "qa_version"
MESSAGE_KIND = "message"

# A group-scoped Q&A variant beats the global answer of the same question in
# that group. Applied as a small similarity boost so the tie is deterministic.
GROUP_PRIORITY_BOOST = 0.01


@dataclass(frozen=True, slots=True)
class Evidence:
    """A retrieved piece of evidence about a question."""

    source_id: str
    label: str
    text: str
    authority: int
    similarity: float
    question: str | None = None
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


def _merge_group_first(
    global_matches: list[VectorMatch],
    group_matches: list[VectorMatch],
    top_k: int,
) -> list[VectorMatch]:
    """Merge global and group Q&A matches, group variants boosted.

    Args:
        global_matches: Matches from the global scope.
        group_matches: Matches scoped to the asking group.
        top_k: Maximum number of matches to return.

    Returns:
        The combined matches, strongest first, group ones boosted by
        ``GROUP_PRIORITY_BOOST`` so an equally good group variant wins.
    """
    merged = [
        VectorMatch(
            id=match.id,
            score=match.score + GROUP_PRIORITY_BOOST,
            metadata=match.metadata,
        )
        for match in group_matches
    ]
    merged.extend(global_matches)
    merged.sort(key=lambda match: match.score, reverse=True)
    return merged[:top_k]


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
        anchor=_opt_text(metadata.get("anchor")),
        url=_opt_text(metadata.get("url")),
        date=_opt_text(metadata.get("date")),
        author=_opt_text(metadata.get("author")),
    )


@dataclass(frozen=True, slots=True)
class RetrievalService:
    """Retrieve evidence from the vector store."""

    embedder: Embedder
    vectors: VectorStore
    qa_top_k: int = 5
    message_top_k: int = 8

    async def retrieve(
        self,
        question: str,
        conversation_id: str | None = None,
    ) -> RetrievedEvidence:
        """Return Q&A and message evidence for a question.

        Evidence is scoped: Q&A matches come from the global layer plus the
        asking group's own knowledge; messages come only from the asking group.
        Without a conversation id (evals) no scope filter applies.

        Args:
            question: The user question.
            conversation_id: The asking group, when known.

        Returns:
            The retrieved evidence, strongest first.
        """
        embeddings = await self.embedder.embed([question])
        vector = embeddings[0] if embeddings else []
        base_filters: dict[str, object] = {"kind": QA_KIND, "status": "active"}
        qa_matches = await self.vectors.query(
            vector,
            top_k=self.qa_top_k,
            filters=base_filters
            if conversation_id is None
            else {**base_filters, "scope": GLOBAL_SCOPE},
        )
        if conversation_id is not None:
            group_matches = await self.vectors.query(
                vector,
                top_k=self.qa_top_k,
                filters={**base_filters, "scope": conversation_id},
            )
            qa_matches = _merge_group_first(qa_matches, group_matches, self.qa_top_k)
        message_filters: dict[str, object] = {"kind": MESSAGE_KIND}
        if conversation_id is not None:
            message_filters["scope"] = conversation_id
        message_matches = await self.vectors.query(
            vector,
            top_k=self.message_top_k,
            filters=message_filters,
        )
        return RetrievedEvidence(
            qa=[_to_evidence(match, QA_KIND) for match in qa_matches],
            messages=[_to_evidence(match, MESSAGE_KIND) for match in message_matches],
        )
