# SPDX-License-Identifier: MIT
"""FTS5 lexical projection shared by both SQL runtimes.

The lexical leg is derived state, rebuildable from SQL, and costs no AI budget
to query. It lives here rather than in the Cloudflare adapter because D1 and
the local runtime speak the same ``SqlDatabase`` protocol and run the same
migrations, so one implementation serves both.
"""

import json
import re
import unicodedata
from itertools import batched

from knowledge_bot.infrastructure.sql.protocol import (
    SqlDatabase,
    SqlResult,
    SqlStatement,
)
from knowledge_bot.ports.index import LexicalMatch, LexicalRecord

#: D1 accepts a bounded number of statements per batch.
_BATCH_CHUNK = 50

#: The lexical leg ORs its content tokens. The removed implementation joined
#: them with spaces, which FTS5 reads as an implicit AND, so every stopword had
#: to be present in a row or the query returned nothing: 2 of 91 eval questions.
_LEXICAL_TOKEN = re.compile(r"[^\W\d_]+", flags=re.UNICODE)

#: Query words carrying no discriminative weight in this corpus. SQLite's own
#: bm25 already zeroes a term's contribution as its document frequency rises, so
#: this list only stops the commonest word of a question from dominating the OR
#: and dragging in an unrelated row.
_LEXICAL_STOPWORDS = frozenset(
    {
        "a",
        "aixi",
        "això",
        "al",
        "alguna",
        "algun",
        "algunes",
        "alguns",
        "als",
        "altra",
        "altre",
        "altres",
        "amb",
        "aquella",
        "aquell",
        "aquest",
        "aquesta",
        "aqui",
        "ara",
        "aun",
        "cada",
        "com",
        "contra",
        "de",
        "del",
        "des",
        "despres",
        "dins",
        "durant",
        "el",
        "ell",
        "ella",
        "elles",
        "els",
        "ells",
        "en",
        "encara",
        "entre",
        "es",
        "esta",
        "estan",
        "estem",
        "estic",
        "esteu",
        "ets",
        "et",
        "fer",
        "fins",
        "ha",
        "han",
        "has",
        "hi",
        "ho",
        "i",
        "la",
        "les",
        "li",
        "llavors",
        "lo",
        "los",
        "m",
        "ma",
        "malgrat",
        "mateixa",
        "mateixes",
        "mateix",
        "me",
        "mes",
        "meva",
        "meves",
        "molts",
        "mon",
        "molt",
        "n",
        "ne",
        "ni",
        "no",
        "nosaltres",
        "nostra",
        "o",
        "on",
        "pel",
        "per",
        "perque",
        "poc",
        "potser",
        "qual",
        "quan",
        "quant",
        "que",
        "quelcom",
        "qui",
        "s",
        "sa",
        "segons",
        "sempre",
        "ses",
        "seu",
        "seva",
        "seves",
        "si",
        "sobre",
        "solament",
        "son",
        "sons",
        "sota",
        "t",
        "tambe",
        "te",
        "tenen",
        "tenir",
        "teu",
        "teva",
        "teves",
        "tot",
        "tota",
        "totes",
        "tots",
        "un",
        "una",
        "unes",
        "uns",
        "va",
        "van",
        "vosaltres",
        "vostra",
    }
)

#: Metadata columns the lexical projection may filter on. Anything else is a
#: caller error rather than a silently ignored clause.
_FTS_FILTER_COLUMNS = frozenset({"kind", "scope_key", "canonical_key"})


def _to_python(value: object) -> object:
    """Convert a binding value that may be a proxy."""
    converter = getattr(value, "to_py", None)
    return converter() if callable(converter) else value


def _rows(result: SqlResult) -> list[dict[str, object]]:
    """Return result rows as plain dicts."""
    raw = getattr(result, "results", None)
    if not isinstance(raw, list):
        return []
    return [dict(row) for row in raw]


def _as_int(value: object) -> int:
    """Coerce a metadata value to int, defaulting to zero."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    return 0


def _fold(token: str) -> str:
    """Fold a token exactly as the FTS5 tokenizer folds the indexed text.

    The table is created with ``remove_diacritics 1``, so the stored text has no
    accents: ``llicència`` is indexed as ``llicencia``. A query folded
    differently would silently never match it, so both sides must agree.
    """
    decomposed = unicodedata.normalize("NFD", token.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def lexical_match_query(query: str) -> str:
    """Turn free text into an OR of its content tokens, safe for FTS5.

    Tokens are quoted so FTS5 cannot read a user word as query syntax, folded
    the way the index folds its text, and filtered of stopwords and very short
    words: an OR of everything is dominated by the commonest word of the
    question, and an AND of everything matches nothing.

    Args:
        query: The raw user or evaluation question.

    Returns:
        An FTS5 MATCH expression, or an empty string when nothing survives.
    """
    tokens = [
        folded
        for token in _LEXICAL_TOKEN.findall(query)
        if (folded := _fold(token)) not in _LEXICAL_STOPWORDS and len(folded) > 2
    ]
    return " OR ".join(f'"{token}"' for token in dict.fromkeys(tokens))


def _json_dumps(value: object) -> str:
    """Serialize a metadata map for the lexical row."""
    return json.dumps(value, separators=(",", ":"), default=str)


def _json_loads(value: object) -> dict[str, object]:
    """Deserialize lexical row metadata defensively."""
    if not isinstance(value, str):
        return {}
    try:
        payload = json.loads(value)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


class SqlLexicalIndex:
    """FTS5 projection of Q&A answer texts over any ``SqlDatabase``.

    Rows are keyed by the stable vector id, so the lexical projection shares the
    vector projection's lifecycle: both are written and replaced together, and
    both are rebuildable from SQL truth.
    """

    def __init__(self, database: SqlDatabase) -> None:
        """Wrap a SQL database binding."""
        self._db = database

    async def upsert(self, records: list[LexicalRecord]) -> None:
        """Replace the lexical row of every given vector id."""
        for chunk in batched(records, _BATCH_CHUNK, strict=False):
            statements: list[SqlStatement] = []
            for record in chunk:
                statements.append(
                    self._db.prepare("DELETE FROM search_fts WHERE vector_id = ?").bind(
                        record.id
                    )
                )
                statements.append(
                    self._db.prepare(
                        "INSERT INTO search_fts"
                        " (vector_id, kind, scope_key, canonical_key, authority,"
                        "  metadata_json, answer_text)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    ).bind(
                        record.id,
                        record.kind,
                        record.scope_key,
                        record.canonical_key,
                        record.authority,
                        _json_dumps(record.metadata),
                        record.text,
                    )
                )
            await self._db.batch(statements)

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        filters: dict[str, object] | None = None,
    ) -> list[LexicalMatch]:
        """Return BM25-ranked matches as positive strengths, best first.

        Args:
            query: Free text; only its content tokens reach the index.
            top_k: Maximum rows to return.
            filters: Exact-match clauses on the unindexed metadata columns.

        Returns:
            Matches ordered best first, with ``score`` as a positive strength.

        Raises:
            ValueError: If a filter names a column that is not filterable.
        """
        match_query = lexical_match_query(query)
        if not match_query:
            return []
        clauses = ["search_fts MATCH ?"]
        parameters: list[object] = [match_query]
        for key, value in (filters or {}).items():
            if key not in _FTS_FILTER_COLUMNS:
                message = f"unsupported lexical filter: {key}"
                raise ValueError(message)
            clauses.append(f"{key} = ?")
            parameters.append(value)
        parameters.append(top_k)
        result = await (
            self._db.prepare(
                "SELECT vector_id, metadata_json, bm25(search_fts) AS strength"
                f" FROM search_fts WHERE {' AND '.join(clauses)}"
                " ORDER BY bm25(search_fts) LIMIT ?"
            )
            .bind(*parameters)
            .run()
        )
        matches: list[LexicalMatch] = []
        for row in _rows(result):
            raw = _to_python(row.get("strength"))
            strength = -float(raw) if isinstance(raw, int | float) else 0.0
            matches.append(
                LexicalMatch(
                    id=str(row["vector_id"]),
                    score=round(strength, 4),
                    metadata=_json_loads(row["metadata_json"]),
                )
            )
        return matches

    async def delete(self, ids: list[str]) -> None:
        """Delete lexical rows by their stable projection ids."""
        for chunk in batched(ids, _BATCH_CHUNK, strict=False):
            await self._db.batch(
                [
                    self._db.prepare("DELETE FROM search_fts WHERE vector_id = ?").bind(
                        vector_id
                    )
                    for vector_id in chunk
                ]
            )
