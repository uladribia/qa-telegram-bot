# SPDX-License-Identifier: MIT
"""Retrieval: embed the question and fetch Q&A and message evidence."""

from dataclasses import dataclass, field

from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.vector_store import VectorMatch, VectorStore

QA_KIND = "qa"
MESSAGE_KIND = "message_evidence"


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
    """Return the canonical question key of a Q&A match, if any.

    Args:
        match: The vector match.

    Returns:
        The canonical key from the match metadata, or ``None``.
    """
    key = match.metadata.get("canonical_key")
    return key if isinstance(key, str) and key else None


def _merge_group_first(
    global_matches: list[VectorMatch],
    group_matches: list[VectorMatch],
    top_k: int,
) -> list[VectorMatch]:
    """Merge global and group Q&A matches, group variants first.

    A group-scoped match always suppresses the global match of the same
    canonical question in that group, regardless of similarity.

    Args:
        global_matches: Matches from the global scope.
        group_matches: Matches scoped to the asking group.
        top_k: Maximum number of matches to return.

    Returns:
        The combined matches, strongest first.
    """
    local_keys = {_question_key(match) for match in group_matches} - {None}
    candidates = [
        *group_matches,
        *(match for match in global_matches if _question_key(match) not in local_keys),
    ]
    candidates.sort(
        key=lambda match: (match.score, _as_int(match.metadata.get("authority"))),
        reverse=True,
    )
    return candidates[:top_k]


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
    """Retrieve evidence from the vector store."""

    embedder: Embedder
    vectors: VectorStore
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
        qa_filters = (
            {**base_filters, "scope_key": GLOBAL_SCOPE}
            if all_scopes is False
            else base_filters
        )
        qa_matches = await self.vectors.query(
            vector, top_k=self.qa_top_k, filters=qa_filters
        )
        if space_id is not None:
            local_matches = await self.vectors.query(
                vector,
                top_k=self.qa_top_k,
                filters={**base_filters, "scope_key": scope_for_space(space_id)},
            )
            qa_matches = _merge_group_first(qa_matches, local_matches, self.qa_top_k)
        else:
            qa_matches.sort(
                key=lambda match: (
                    match.score,
                    _as_int(match.metadata.get("authority")),
                ),
                reverse=True,
            )
        message_filters: dict[str, object] = {"kind": MESSAGE_KIND}
        if space_id is not None:
            message_filters["scope_key"] = scope_for_space(space_id)
        elif all_scopes is False:
            message_filters["scope_key"] = GLOBAL_SCOPE
        message_matches = await self.vectors.query(
            vector,
            top_k=self.message_top_k,
            filters=message_filters,
        )
        return RetrievedEvidence(
            qa=[_to_evidence(match, QA_KIND) for match in qa_matches],
            messages=[_to_evidence(match, MESSAGE_KIND) for match in message_matches],
        )
