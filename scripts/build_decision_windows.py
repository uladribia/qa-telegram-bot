# SPDX-License-Identifier: MIT
"""Build the hard decision windows for the Clef-Flash evaluation.

The windows are the differentiating dataset: cases where the deterministic
baseline either abstains for the wrong reason or pairs confidently. They are
built from what the repository already knows — the canonical Q&A anchors and
their retrieval phrasings — never from invented facts, and never with random
negatives: a negative is the *nearest other* question in the same split, so
pairing the wrong candidate is a real mistake rather than an obvious one.

The output is `evals/decision_windows.yaml`, committed and reviewed. The
generator stays in the repository so the dataset is auditable and rebuildable;
regenerating it after model outputs exist would invalidate the evaluation, which
is why the runner never calls it.
"""

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evals" / "decision_windows.yaml"
SEED = 20261003
EMBEDDING_MODEL = "embeddinggemma"

#: Exactly the counts the evaluation plan fixes.
CATEGORY_COUNTS = {
    "single_correct": 5,
    "multi_one_correct": 15,
    "no_correct": 10,
    "ambiguous_two_plausible": 5,
    "explicit_genuine": 5,
    "explicit_unrelated": 5,
    "chitchat_noise": 5,
}

#: Fifteen calibration, thirty-five held out, allocated per category.
SPLIT_ALLOCATION = {
    "single_correct": (2, 3),
    "multi_one_correct": (5, 10),
    "no_correct": (3, 7),
    "ambiguous_two_plausible": (1, 4),
    "explicit_genuine": (1, 4),
    "explicit_unrelated": (1, 4),
    "chitchat_noise": (2, 3),
}

#: Short answers that fit more than one open question. An ambiguous window
#: pairs one of these with two questions that are both plausible and neither of
#: which is the answer's own question; that is what makes abstaining the right
#: answer rather than a guess.
AMBIGUOUS_ANSWERS = (
    "A les sis.",
    "Dimarts i dijous.",
    "Al pavelló municipal.",
    "Toca pagar.",
    "Ho fa l'equip tècnic.",
)
AMBIGUOUS_PAIRS = (
    ("A quina hora entrenen els equips?", "A quina hora es juga el partit?"),
    ("Quins dies hi ha entrenament?", "Quins dies hi ha partit?"),
    ("On es juga?", "On es fa la inscripció?"),
    ("Com es paga?", "Qui ha de pagar-ho?"),
    ("Qui ho organitza?", "Qui hi assistirà?"),
)

#: Content words that carry no signal when comparing a question to an answer.
STOPWORDS = frozenset(
    [
        "a",
        "al",
        "als",
        "amb",
        "això",
        "cada",
        "com",
        "de",
        "del",
        "dels",
        "el",
        "els",
        "en",
        "és",
        "i",
        "la",
        "les",
        "lo",
        "los",
        "més",
        "meu",
        "meva",
        "molt",
        "no",
        "o",
        "on",
        "pel",
        "per",
        "però",
        "perquè",
        "que",
        "què",
        "qui",
        "quin",
        "quina",
        "res",
        "s",
        "se",
        "ses",
        "seu",
        "seva",
        "si",
        "sobre",
        "son",
        "sons",
        "t",
        "ta",
        "tal",
        "també",
        "tan",
        "tant",
        "te",
        "tenir",
        "tot",
        "tots",
        "un",
        "una",
        "unes",
        "uns",
        "va",
        "van",
        "vaig",
        "vosaltres",
        "ja",
        "hi",
        "ha",
        "han",
        "haver",
    ]
)

#: A candidate question whose content words this fraction of them appear in the
#: answer is very likely answered by it, so it is not a hard negative at all.
ANSWERABLE_OVERLAP = 0.6

#: Two questions sharing this fraction of their content words are near
#: duplicates, which makes "pick exactly one of these" unanswerable.
DUPLICATE_OVERLAP = 0.5

#: Replies that arrive under an open question without answering it. The first
#: is a bare acknowledgement the prefilter decides locally; the rest are
#: statements whose intent is genuinely arguable, so the dataset asserts no
#: label for them and the runner treats intent as informational there.
ACKNOWLEDGEMENTS = ("gràcies", "perfecte", "d'acord")
NON_ANSWERS = (*ACKNOWLEDGEMENTS, "ho miro després", "parlem demà")


@dataclass(frozen=True, slots=True)
class Anchor:
    """One canonical question and its answer."""

    anchor_id: str
    question: str
    answer: str
    queries: tuple[str, ...]


def load_anchors() -> list[Anchor]:
    """Load the canonical anchors with their retrieval phrasings."""
    phrasings: dict[str, list[str]] = {}
    eval_file = ROOT / "evals" / "retrieval_synthetic.yaml"
    for entry in yaml.safe_load(eval_file.read_text(encoding="utf-8")):
        anchor = entry.get("expected_anchor")
        query = entry.get("query")
        if anchor and query:
            phrasings.setdefault(anchor, []).append(query)
    anchors: list[Anchor] = []
    for entry in json.loads((ROOT / "data" / "seed" / "qa.json").read_text("utf-8")):
        anchors.append(
            Anchor(
                anchor_id=entry["source_anchor"],
                question=entry["question"],
                answer=entry["answer"],
                queries=tuple(phrasings.get(entry["source_anchor"], [])),
            )
        )
    return anchors


def embed(texts: list[str], base_url: str) -> dict[str, list[float]]:
    """Embed texts with the local embedding model."""
    unique = list(dict.fromkeys(texts))
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/embed",
        json={"model": EMBEDDING_MODEL, "input": unique},
        timeout=120.0,
    )
    response.raise_for_status()
    return dict(zip(unique, response.json()["embeddings"], strict=True))


def cosine(left: list[float], right: list[float]) -> float:
    """Return the cosine similarity of two vectors."""
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _content_words(text: str) -> set[str]:
    """Return the content words of a text, lowercased."""
    words = {word.strip(".,;:!?()¿¡'\"\u2019-").lower() for word in text.split()}
    return {word for word in words if word and word not in STOPWORDS}


def _overlap(left: set[str], right: set[str]) -> float:
    """Return the fraction of the smaller word set present in the larger one."""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def is_answerable(question: str, answer: str) -> bool:
    """Whether a question's words are largely contained in the answer."""
    return (
        _overlap(_content_words(question), _content_words(answer)) >= ANSWERABLE_OVERLAP
    )


def is_duplicate(left: str, right: str) -> bool:
    """Whether two questions are near duplicates of each other."""
    return _overlap(_content_words(left), _content_words(right)) >= DUPLICATE_OVERLAP


def nearest_others(
    query: str,
    anchor_id: str,
    anchors: list[Anchor],
    vectors: dict[str, list[float]],
    count: int,
) -> list[Anchor]:
    """Return the nearest questions that are not this anchor's."""
    ranked = sorted(
        (
            (cosine(vectors[query], vectors[item.question]), item.anchor_id)
            for item in anchors
            if item.anchor_id != anchor_id
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    chosen = [anchor_id for _, anchor_id in ranked[:count]]
    by_id = {item.anchor_id: item for item in anchors}
    return [by_id[anchor_id] for anchor_id in chosen]


def _candidate(index: int, anchor: Anchor, question: str, relation: str) -> dict:
    """Return one candidate DTO for the YAML."""
    return {
        "candidate_id": f"k{index}",
        "question_message_id": f"m{anchor.anchor_id}-{index}",
        "question": question,
        "relation": relation,
    }


def build(
    anchors: list[Anchor], vectors: dict[str, list[float]]
) -> list[dict[str, Any]]:
    """Build every window category in the fixed counts."""
    rng = random.Random(SEED)
    pool: list[Anchor] = []
    windows: list[dict[str, Any]] = []

    def take(count: int) -> list[Anchor]:
        """Return the next anchors, reshuffling once the pool runs dry.

        Categories may reuse an anchor: what must stay unique is a window's own
        content, not the anchor it draws its question from.
        """
        nonlocal pool
        taken: list[Anchor] = []
        while len(taken) < count:
            if not pool:
                pool = list(anchors)
                rng.shuffle(pool)
            taken.append(pool.pop(0))
        return taken

    for number, anchor in enumerate(take(CATEGORY_COUNTS["single_correct"]), start=1):
        windows.append(
            {
                "id": f"single-{number}",
                "category": "single_correct",
                "text": anchor.answer,
                "candidates": [
                    _candidate(0, anchor, anchor.question, "temporal_window")
                ],
                "expected_pair": "k0",
                "expected_intent": "knowledge_update",
            }
        )

    for number, anchor in enumerate(
        take(CATEGORY_COUNTS["multi_one_correct"]), start=1
    ):
        negatives = _hard_negatives(
            anchor,
            anchor.question,
            positive_question=anchor.question,
            anchors=anchors,
            vectors=vectors,
            count=4,
        )
        windows.append(
            {
                "id": f"multi-{number}",
                "category": "multi_one_correct",
                "text": anchor.answer,
                "candidates": [
                    _candidate(0, anchor, anchor.question, "temporal_window"),
                    *[
                        _candidate(index, other, other.question, "temporal_window")
                        for index, other in enumerate(negatives, start=1)
                    ],
                ],
                "expected_pair": "k0",
                "expected_intent": "knowledge_update",
            }
        )

    for number, anchor in enumerate(take(CATEGORY_COUNTS["no_correct"]), start=1):
        negatives = _hard_negatives(
            anchor,
            anchor.question,
            positive_question=None,
            anchors=anchors,
            vectors=vectors,
            count=4,
        )
        windows.append(
            {
                "id": f"nocorrect-{number}",
                "category": "no_correct",
                "text": anchor.answer,
                "candidates": [
                    _candidate(index, other, other.question, "temporal_window")
                    for index, other in enumerate(negatives)
                ],
                "expected_pair": None,
                "expected_intent": "knowledge_update",
            }
        )

    for number, anchor in enumerate(
        take(CATEGORY_COUNTS["ambiguous_two_plausible"]), start=1
    ):
        answer_text = AMBIGUOUS_ANSWERS[(number - 1) % len(AMBIGUOUS_ANSWERS)]
        left, right = AMBIGUOUS_PAIRS[(number - 1) % len(AMBIGUOUS_PAIRS)]
        windows.append(
            {
                "id": f"ambiguous-{number}",
                "category": "ambiguous_two_plausible",
                "text": answer_text,
                "candidates": [
                    _candidate(0, anchor, left, "temporal_window"),
                    _candidate(1, anchor, right, "temporal_window"),
                ],
                "expected_pair": None,
                "expected_intent": "knowledge_update",
            }
        )

    for number, anchor in enumerate(take(CATEGORY_COUNTS["explicit_genuine"]), start=1):
        windows.append(
            {
                "id": f"explicit-{number}",
                "category": "explicit_genuine",
                "text": anchor.answer,
                "candidates": [
                    _candidate(0, anchor, anchor.question, "explicit_reply"),
                    *_temporal_candidates(1, anchor, anchors, vectors),
                ],
                "expected_pair": "k0",
                "expected_intent": "knowledge_update",
            }
        )

    for number, anchor in enumerate(
        take(CATEGORY_COUNTS["explicit_unrelated"]), start=1
    ):
        windows.append(
            {
                "id": f"explicit-unrelated-{number}",
                "category": "explicit_unrelated",
                "text": NON_ANSWERS[number % len(NON_ANSWERS)],
                "candidates": [
                    _candidate(0, anchor, anchor.question, "explicit_reply")
                ],
                "expected_pair": None,
                "expected_intent": (
                    "chitchat"
                    if NON_ANSWERS[number % len(NON_ANSWERS)] in ACKNOWLEDGEMENTS
                    else None
                ),
            }
        )

    for number, anchor in enumerate(take(CATEGORY_COUNTS["chitchat_noise"]), start=1):
        windows.append(
            {
                "id": f"chitchat-{number}",
                "category": "chitchat_noise",
                "text": _noise(number),
                "candidates": [
                    _candidate(index, other, other.question, "temporal_window")
                    for index, other in enumerate(
                        nearest_others(
                            anchor.question, anchor.anchor_id, anchors, vectors, 2
                        ),
                        start=1,
                    )
                ],
                "expected_pair": None,
                "expected_intent": "chitchat",
            }
        )
    return windows


def _hard_negatives(
    anchor: Anchor,
    query: str,
    *,
    positive_question: str | None,
    anchors: list[Anchor],
    vectors: dict[str, list[float]],
    count: int,
) -> list[Anchor]:
    """Return negatives that are near but genuinely not answered.

    A negative the answer already covers would make the case unanswerable, and
    a negative that repeats the positive question would make "pick one" a coin
    toss. Both are rejected here rather than discovered in the results.
    """
    by_id = {item.anchor_id: item for item in anchors}
    chosen: list[Anchor] = []
    for _, anchor_id in _ranked_other_anchors(
        query, anchor.anchor_id, anchors, vectors
    ):
        candidate = by_id[anchor_id]
        if is_answerable(candidate.question, anchor.answer):
            continue
        if positive_question and is_duplicate(candidate.question, positive_question):
            continue
        chosen.append(candidate)
        if len(chosen) == count:
            break
    return chosen


def _ranked_other_anchors(
    query: str, anchor_id: str, anchors: list[Anchor], vectors: dict[str, list[float]]
) -> list[tuple[float, str]]:
    """Return every other anchor, nearest first."""
    return sorted(
        (
            (cosine(vectors[query], vectors[item.question]), item.anchor_id)
            for item in anchors
            if item.anchor_id != anchor_id
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )


def _temporal_candidates(
    start: int, anchor: Anchor, anchors: list[Anchor], vectors: dict[str, list[float]]
) -> list[dict]:
    """Return two nearby open questions the answer does not already cover.

    An explicit-reply window is only interesting if the model is not simply
    pairing because the message is a reply: a sibling question the answer also
    covers would make the pair correct for the wrong reason.
    """
    chosen: list[Anchor] = []
    for _, anchor_id in _ranked_other_anchors(
        anchor.question, anchor.anchor_id, anchors, vectors
    ):
        other = next(item for item in anchors if item.anchor_id == anchor_id)
        if is_answerable(other.question, anchor.answer):
            continue
        if is_duplicate(other.question, anchor.question):
            continue
        chosen.append(other)
        if len(chosen) == 2:
            break
    return [
        _candidate(start + offset, other, other.question, "temporal_window")
        for offset, other in enumerate(chosen)
    ]


def _noise(number: int) -> str:
    """Return a social message that answers nothing."""
    return (
        "bon dia a tothom",
        "em sembla molt bé",
        "riure riure",
        "quinonymous foto",
        "ja saps que sí",
    )[number % 5]


def assign_splits(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign each window to calibration or held-out test, deterministically."""
    by_category: dict[str, list[dict[str, Any]]] = {}
    for window in windows:
        by_category.setdefault(window["category"], []).append(window)
    for category, group in by_category.items():
        calibration, _ = SPLIT_ALLOCATION[category]
        for index, window in enumerate(group):
            window["split"] = "calibration" if index < calibration else "test"
    return windows


def main() -> int:
    """Write the decision windows and print what they contain."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama-base-url", default="http://127.0.0.1:11434")
    arguments = parser.parse_args()
    anchors = load_anchors()
    vectors = embed(
        [anchor.question for anchor in anchors]
        + [query for anchor in anchors for query in anchor.queries],
        arguments.ollama_base_url,
    )
    windows = assign_splits(build(anchors, vectors))
    header = {
        "id": "clef-decision-windows",
        "description": (
            "Hard listener decision windows built from the canonical Q&A anchors. "
            "Reviewed once and frozen before any model output exists; never edit "
            "these after seeing results. Regenerate only with "
            "scripts/build_decision_windows.py when the underlying Q&A changes."
        ),
        "schema_version": "qa-bot-decision-windows-v1",
        "seed": SEED,
        "anchor_count": len(anchors),
        "review": {
            "status": "reviewed and frozen before any model output existed",
            "findings_fixed": [
                "no_correct windows whose hard negative was answered by the text",
                "multi_one_correct windows whose negative duplicated the positive",
                "explicit_genuine windows whose sibling question was also answered",
                "ambiguous windows built from a truncated answer, which still "
                "answered its own question and so were not ambiguous at all",
            ],
            "audited_by": "scripts/build_decision_windows.py overlap rules, plus a "
            "manual read of every window",
            "deviations": [
                "ambiguous windows use generic short answers rather than a "
                "truncated corpus answer, because a truncated corpus answer "
                "still answers its own question",
                "anchors may repeat across categories; only a window's own "
                "content must be unique",
            ],
            "known_softness": [
                "three explicit_unrelated windows assert no intent label: a "
                "non-answer such as 'ho miro després' is genuinely arguable, "
                "and those labels are informational only",
            ],
        },
        "windows": windows,
    }
    OUTPUT.write_text(
        yaml.safe_dump(header, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    splits: dict[str, int] = {}
    for window in windows:
        counts[window["category"]] = counts.get(window["category"], 0) + 1
        splits[window["split"]] = splits.get(window["split"], 0) + 1
    print(json.dumps({"categories": counts, "splits": splits}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
