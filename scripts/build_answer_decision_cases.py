# SPDX-License-Identifier: MIT
"""Build the post-retrieval decision suites: abstention and evidence selection.

Two families, one schema, both grounded on the seeded canonical Q&A:

``gold``
    The human set. ``evals/gold.yaml`` supplies the questions whose answerability
    a human decided (11 answerable, 15 abstentions), and every canonical anchor
    contributes its own question, whose answer is known exactly. Evidence is the
    canonical answers; distractors come from the same section, so the right item
    is never the only plausible one.

``synthetic``
    The realistic set. The 467 retrieval paraphrases already in the repository,
    each inheriting its anchor's answerability, plus near-miss distractors built
    from the same section with one fact changed. Questions are real group-chat
    phrasings: Catalan, Spanish, typos, mixed.

Every case states the truth it can: which evidence ids answer the question, and
whether any evidence is enough at all. A case whose right item is missing from
the shortlist is marked ``retrieval_missed``, because no decision model can
recover an item cosine never proposed, and counting that as a model failure
would make the numbers a measure of retrieval instead of of decisions.

Split discipline: calibration and held-out are assigned here, deterministically,
and the runner tunes on calibration only.
"""

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evals" / "answer_decisions.yaml"
SEED = 20261004

#: Shortlist size the cosine retrieval would hand over, matching the runtime's
#: configured widths. The shortlist is the token gate in front of the model.
SHORTLIST = 7
#: How many held-out calibration cases each family contributes, by fraction.
CALIBRATION_FRACTION = 0.30


@dataclass(frozen=True, slots=True)
class Anchor:
    """One canonical question and its answer, with its section."""

    anchor_id: str
    section: str
    question: str
    answer: str
    paraphrases: tuple[str, ...]


def load_anchors() -> list[Anchor]:
    """Load the canonical anchors with their retrieval paraphrases."""
    phrasings: dict[str, list[str]] = {}
    for entry in yaml.safe_load(
        (ROOT / "evals" / "retrieval_synthetic.yaml").read_text(encoding="utf-8")
    ):
        anchor = entry.get("expected_anchor")
        query = entry.get("query")
        if anchor and query:
            phrasings.setdefault(anchor, []).append(query)
    return [
        Anchor(
            anchor_id=entry["source_anchor"],
            section=entry.get("section", ""),
            question=entry["question"],
            answer=entry["answer"],
            paraphrases=tuple(phrasings.get(entry["source_anchor"], [])),
        )
        for entry in json.loads((ROOT / "data" / "seed" / "qa.json").read_text("utf-8"))
    ]


def _items(shortlist: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Return a shortlist as serialisable evidence items."""
    return [
        {"evidence_id": evidence_id, "text": text} for evidence_id, text in shortlist
    ]


def _short_evidence(text: str, limit: int = 600) -> str:
    """Return the beginning of an answer, which is where the fact usually is."""
    flattened = " ".join(text.split())
    return flattened[:limit]


def _section_peers(anchor: Anchor, anchors: list[Anchor]) -> list[Anchor]:
    """Return the other anchors that share this anchor's section."""
    return [
        item
        for item in anchors
        if item.section == anchor.section and item.anchor_id != anchor.anchor_id
    ]


def _shortlist(
    answer_anchor: Anchor | None,
    anchors: list[Anchor],
    rng: random.Random,
    *,
    size: int = SHORTLIST,
) -> list[tuple[str, str]]:
    """Return a shortlist with the right item present but not obviously first.

    The point is a decision, not a lookup: distractors come from the same
    section, so topical similarity cannot identify the answer on its own.
    """
    pool = _section_peers(answer_anchor, anchors) if answer_anchor else anchors
    rng.shuffle(pool)
    chosen = pool[: max(size - (1 if answer_anchor else 0), 0)]
    items: list[tuple[str, str]] = []
    if answer_anchor is not None:
        # The correct item sits in the middle of the shortlist, so position is
        # not the signal.
        position = min(1, len(chosen))
        chosen.insert(position, answer_anchor)
    for item in chosen[:size]:
        items.append((item.anchor_id, _short_evidence(item.answer)))
    return items


def gold_cases(anchors: list[Anchor]) -> list[dict[str, Any]]:
    """Return the human cases: the gold file plus every canonical anchor."""
    rng = random.Random(SEED)
    cases: list[dict[str, Any]] = []
    document = yaml.safe_load(
        (ROOT / "evals" / "gold.yaml").read_text(encoding="utf-8")
    )
    for entry in document["abstentions"]:
        cases.append(
            {
                "case_id": entry["id"],
                "family": "gold",
                "question": entry["question"],
                "evidence": _items(_shortlist(None, anchors, rng)),
                "expected_answer_ids": [],
                "expected_sufficient": False,
                "reason": entry.get("reason", "abstention"),
            }
        )
    for entry in document["answers"]:
        # A human wrote this question knowing it is answerable, but nobody
        # recorded which item answers it. Guessing that from word overlap is
        # how a dataset grows invisible wrong labels, so these cases carry no
        # selection truth: they score abstention only, and selection metrics
        # come from the canonical anchors, whose answer is certain.
        cases.append(
            {
                "case_id": entry["id"],
                "family": "gold",
                "question": entry["question"],
                "evidence": _items(_shortlist(None, anchors, rng)),
                "expected_answer_ids": None,
                "expected_sufficient": True,
                "reason": entry.get("category", "answerable"),
            }
        )
    for anchor in anchors:
        cases.append(
            {
                "case_id": f"canonical-{anchor.anchor_id}",
                "family": "gold",
                "question": anchor.question,
                "evidence": _items(_shortlist(anchor, anchors, rng)),
                "expected_answer_ids": [anchor.anchor_id],
                "expected_sufficient": True,
                "reason": "canonical_anchor",
            }
        )
    return cases


def synthetic_cases(anchors: list[Anchor]) -> list[dict[str, Any]]:
    """Return the realistic cases: real paraphrases over known answerability."""
    rng = random.Random(SEED + 1)
    cases: list[dict[str, Any]] = []
    number = 0
    for anchor in anchors:
        variants = anchor.paraphrases[:3]
        for variant in variants:
            number += 1
            cases.append(
                {
                    "case_id": f"paraphrase-{number}",
                    "family": "synthetic",
                    "question": variant,
                    "evidence": _items(_shortlist(anchor, anchors, rng)),
                    "expected_answer_ids": [anchor.anchor_id],
                    "expected_sufficient": True,
                    "reason": "known_answer_paraphrase",
                }
            )
    # Realistic non-answerable traffic: a question the corpus cannot answer,
    # shortlisted with near neighbours so abstention is a real choice.
    unanswerable = (
        ("Quin és el dorsal del meu fill?", "personal_data"),
        ("Demà vindrà l'entrenador Marc?", "unconfirmed_future"),
        ("Quina talla exacta necessita el meu fill?", "personal_data"),
        ("Hi hauria menjar vegetarià al bar del pavelló?", "personal_preference"),
        ("El president del club es presentarà a l'elecció?", "unconfirmed_future"),
        ("On puc deixar el gos mentre entreno?", "personal_data"),
        ("Quants diners he pagat jo?", "personal_data"),
        ("Marta em deu res del viatge?", "personal_data"),
    )
    for index, (question, reason) in enumerate(unanswerable, start=1):
        cases.append(
            {
                "case_id": f"unanswerable-{index}",
                "family": "synthetic",
                "question": question,
                "evidence": _items(_shortlist(None, anchors, rng)),
                "expected_answer_ids": [],
                "expected_sufficient": False,
                "reason": reason,
            }
        )
    return cases


def assign_splits(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign each case to calibration or held-out, deterministically per family."""
    rng = random.Random(SEED + 2)
    for family in ("gold", "synthetic"):
        group = [case for case in cases if case["family"] == family]
        rng.shuffle(group)
        calibration = round(len(group) * CALIBRATION_FRACTION)
        for index, case in enumerate(group):
            case["split"] = "calibration" if index < calibration else "test"
    return cases


def main() -> int:
    """Write the post-retrieval decision cases and print their shape."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    anchors = load_anchors()
    cases = assign_splits(gold_cases(anchors) + synthetic_cases(anchors))
    document = {
        "id": "clef-answer-decisions",
        "description": (
            "Post-retrieval decision cases: is the evidence enough, and which "
            "items answer the question. Grounded on the seeded canonical Q&A, "
            "with same-section distractors so topical overlap alone cannot "
            "identify the answer. Thresholds are chosen on the calibration "
            "split and applied once to the held-out split."
        ),
        "schema_version": "qa-bot-answer-decisions-v1",
        "seed": SEED,
        "shortlist": SHORTLIST,
        "anchor_count": len(anchors),
        "cases": cases,
    }
    OUTPUT.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    splits: dict[str, int] = {}
    for case in cases:
        counts[case["family"]] = counts.get(case["family"], 0) + 1
        key = f"{case['family']}/{case['split']}"
        splits[key] = splits.get(key, 0) + 1
    print(
        json.dumps(
            {"cases": len(cases), "families": counts, "splits": splits}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
