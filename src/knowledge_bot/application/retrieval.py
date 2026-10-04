# SPDX-License-Identifier: MIT
"""Retrieval: one embedding per question, plus a lexical leg over answer text.

The question is embedded once and the vector index returns its nearest Q&A and
group-evidence candidates. A single cosine threshold decides what evidence
exists at all, and the nearest few above it are what the model is given. Local
Q&A variants suppress global ones for the same canonical question.

A second, subordinate leg runs BM25 over Q&A **answer** text. It exists because
the distinctive terms of this knowledge base live in the answers, not in the
canonical questions: measured in production, "Cluber" appeared in 5 answers and
0 questions, and the venue entry's answer contains "camp" while its question
does not. A lexical index over questions cannot reach those facts. It is
subordinate on purpose: it only runs when the semantic pool already cleared the
floor, so it can add context but can never authorise an answer on its own.

There is no cross-encoder. One was measured and removed: it thinned the prompt
enough to make the bot worse. See [docs/experiments.md](docs/experiments.md).
"""

import re
from dataclasses import dataclass, field

from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.index import LexicalIndex, LexicalMatch
from knowledge_bot.ports.vector_store import VectorMatch, VectorStore

QA_KIND = "qa"
MESSAGE_KIND = "message_evidence"

_CANDIDATE_POOL = 15
#: The lexical leg filters after the database returns rows, so it asks for more
#: than it may keep and discards the rest locally.
_LEXICAL_OVERSAMPLE = 4
_TOKEN = re.compile(r"\w+", flags=re.UNICODE)

#: Authority is a 0-100 score. These bounds normalise it onto the cosine's
#: 0..1 scale so the two can be averaged; production carries 30, 90, and 100.
_AUTHORITY_MIN = 0.0
_AUTHORITY_MAX = 100.0

#: Weight of the semantic score in the blended ranking; authority takes the
#: remainder. Equal weights because authority is a deliberate editorial signal
#: (30 inferred from chat, 90 club-published, 100 authoritative) that cosine
#: cannot see at all.
_SEMANTIC_WEIGHT = 0.5


@dataclass(frozen=True, slots=True)
class Evidence:
    """A retrieved piece of evidence about a question."""

    source_id: str
    label: str
    text: str
    authority: int
    similarity: float
    #: Whether the club published this or it was inferred from conversation.
    #: ``official`` is curated knowledge, ``reported`` is group evidence that
    #: may be noise, hearsay or out of date. The generator is told which is
    #: which so it can prefer the club's own words, but the distinction never
    #: licenses an answer the text does not support.
    provenance: str = "official"
    question: str | None = None
    qa_item_id: str | None = None
    qa_version_id: str | None = None
    anchor: str | None = None
    url: str | None = None
    date: str | None = None
    author: str | None = None
    #: Connector-declared source type, passed through without interpretation.
    source_kind: str | None = None
    #: The knowledge scope this piece of evidence lives in, as stored on the
    #: projection row. It is what tells a multi-scope answer which groups
    #: actually contributed to it, as opposed to which scopes were searched.
    scope_key: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievedEvidence:
    """Q&A and message evidence for a question."""

    qa: list[Evidence] = field(default_factory=list)
    messages: list[Evidence] = field(default_factory=list)
    #: Lexical hits kept after the gates, kept separately for the durable trace
    #: so a retrieval decision can be attributed after the fact.
    lexical_qa: list[Evidence] = field(default_factory=list)

    def all(self) -> list[Evidence]:
        """Return all evidence."""
        return [*self.qa, *self.messages]


def _normalized_authority(match: VectorMatch) -> float:
    """Return a match's authority mapped onto 0..1, clamped to the scale.

    Args:
        match: The candidate whose metadata carries the authority.

    Returns:
        The normalised authority. A missing or unparsable value is 0, which
        never promotes a candidate on its own.
    """
    authority = _as_int(match.metadata.get("authority"))
    span = _AUTHORITY_MAX - _AUTHORITY_MIN
    return min(max((authority - _AUTHORITY_MIN) / span, 0.0), 1.0)


def blended_score(match: VectorMatch) -> float:
    """Return the ranking score: cosine and authority, averaged 50/50.

    Blending reorders the candidates that already cleared the floor; it does
    not decide which candidates exist. That separation is deliberate: the
    floor and the gated recall metric are calibrated on the raw cosine, and
    mixing authority into admission would silently move both.

    Args:
        match: The candidate to score.

    Returns:
        The blended score, used for ordering only.
    """
    return _SEMANTIC_WEIGHT * match.score + (
        1.0 - _SEMANTIC_WEIGHT
    ) * _normalized_authority(match)


def _rank(matches: list[VectorMatch], top_k: int) -> list[VectorMatch]:
    """Order candidates by blended score, then cosine, then authority."""
    ranked = sorted(
        matches,
        key=lambda match: (
            blended_score(match),
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


def _merge_lexical(
    semantic: list[VectorMatch],
    lexical: list[VectorMatch],
) -> list[VectorMatch]:
    """Union both Q&A lists, keeping one copy of each item.

    A Q&A found by both legs is one fact, and two near-identical sources is the
    ``redundant_sources`` shape the production generator already fails. The
    semantic copy wins: its score is the cosine the floor and the gated recall
    metric are calibrated on.
    """
    merged = list(semantic)
    seen = {match.id for match in semantic}
    for match in lexical:
        if match.id in seen:
            continue
        seen.add(match.id)
        merged.append(match)
    return merged


def _to_vector_match(match: LexicalMatch, authority: int) -> VectorMatch:
    """Present a lexical hit as an evidence candidate with its own authority.

    Provenance stays official because the text is the club's own curated answer;
    only the match is unverified, and the prompt keeps authority from ever
    licensing an answer. The id is left clean so a Q&A found by both legs
    deduplicates to one citable source.
    """
    metadata = dict(match.metadata)
    metadata["authority"] = authority
    return VectorMatch(id=match.id, score=match.score, metadata=metadata)


def _to_evidence(match: VectorMatch, kind: str) -> Evidence:
    metadata = match.metadata
    question = metadata.get("question")
    official = kind == QA_KIND
    return Evidence(
        source_id=match.id,
        label="Q&A" if official else "Grup",
        provenance="official" if official else "reported",
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
        source_kind=_opt_text(metadata.get("source_kind")),
        scope_key=_opt_text(metadata.get("scope_key")),
    )


@dataclass(frozen=True, slots=True)
class RetrievalService:
    """Retrieve evidence from the vector projection by cosine similarity."""

    embedder: Embedder
    vectors: VectorStore
    lexical: LexicalIndex | None = None
    qa_top_k: int = 5
    message_top_k: int = 2
    floor: float = 0.35
    qa_answer_top_k: int = 3
    qa_answer_min_strength: float = 0.5
    qa_answer_relative_cut: float = 0.5
    lexical_authority: int = 45

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
        lexical_matches = await self._lexical_qa(question, space_id, qa_lists)
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
            qa=[
                _to_evidence(match, QA_KIND)
                for match in _merge_lexical(qa_matches, lexical_matches)
            ],
            messages=[_to_evidence(match, MESSAGE_KIND) for match in message_matches],
            lexical_qa=[_to_evidence(match, QA_KIND) for match in lexical_matches],
        )

    async def _lexical_qa(
        self,
        question: str,
        space_id: str | None,
        semantic_lists: list[list[VectorMatch]],
    ) -> list[VectorMatch]:
        """Return BM25 hits over Q&A answer text, subject to four gates.

        Gate 0, subordination: the leg only runs when the semantic pool already
        produced a candidate at or above the floor. If nothing in the semantic
        pool cleared it, the question is off-topic for the whole knowledge base,
        and three lexical hits on a shared word would turn a correct abstention
        into a confident wrong answer.

        Gate 1, absolute strength: SQLite's bm25 zeroes a term's contribution as
        its document frequency rises, so a near-zero total means the query
        matched only ubiquitous words.

        Gate 2, relative cut: keep hits within a fraction of this query's own
        best hit, which drops the weak tail without an absolute relevance bar.

        Gate 3 is ``qa_answer_top_k`` itself.
        """
        if self.lexical is None or self.qa_answer_top_k < 1:
            return []
        pooled = [match for ranked in semantic_lists for match in ranked]
        if not any(match.score >= self.floor for match in pooled):
            return []
        filters: dict[str, object] = {"kind": QA_KIND}
        if space_id is not None:
            filters["scope_key"] = scope_for_space(space_id)
        else:
            filters["scope_key"] = GLOBAL_SCOPE
        found = await self.lexical.search(
            question,
            top_k=self.qa_answer_top_k * _LEXICAL_OVERSAMPLE,
            filters=filters,
        )
        strong = [
            match for match in found if match.score >= self.qa_answer_min_strength
        ]
        if not strong:
            return []
        best = strong[0].score
        kept = [
            match
            for match in strong
            if match.score >= best * self.qa_answer_relative_cut
        ]
        return [
            _to_vector_match(match, self.lexical_authority)
            for match in kept[: self.qa_answer_top_k]
        ]


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
