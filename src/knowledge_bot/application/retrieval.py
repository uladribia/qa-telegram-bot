# SPDX-License-Identifier: MIT
"""Retrieval: embed the question and fetch Q&A and message evidence."""

from dataclasses import dataclass, field

from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.vector_store import VectorMatch, VectorStore

QA_KIND = "qa_version"
MESSAGE_KIND = "message"


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

    async def retrieve(self, question: str) -> RetrievedEvidence:
        """Return Q&A and message evidence for a question.

        Args:
            question: The user question.

        Returns:
            The retrieved evidence, strongest first.
        """
        embeddings = await self.embedder.embed([question])
        vector = embeddings[0] if embeddings else []
        qa_matches = await self.vectors.query(
            vector,
            top_k=self.qa_top_k,
            filters={"kind": QA_KIND, "status": "active"},
        )
        message_matches = await self.vectors.query(
            vector,
            top_k=self.message_top_k,
            filters={"kind": MESSAGE_KIND},
        )
        return RetrievedEvidence(
            qa=[_to_evidence(match, QA_KIND) for match in qa_matches],
            messages=[_to_evidence(match, MESSAGE_KIND) for match in message_matches],
        )
