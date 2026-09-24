# SPDX-License-Identifier: MIT
"""Generate deterministic synthetic retrieval-eval queries.

Source of truth: the anchors already present in ``evals/retrieval.yaml`` and
``data/seed/qa.json``. No new anchors, no new factual answers: only Catalan,
Spanish, and mixed paraphrases, typos, keyword queries, and competing intents.
The total across both files is exactly 500 queries and no generated query may
appear in the classifier training data.
"""

import json
import random
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TOTAL = 500
SEED = 20260101

# Function-word swap table for realistic mixed Catalan/Spanish chat queries.
_MIXED = {
    "quan": "cuando",
    "on": "donde",
    "com": "cómo",
    "quant": "cuánto",
    "quina": "qué",
    "quin": "qué",
    "quines": "qué",
    "quins": "qué",
    "qui": "quién",
    "perquè": "por qué",
    "quant costa": "cuánto cuesta",
    "hi ha": "hay",
    "cal": "hace falta",
    "es pot": "se puede",
    "hem de": "tenemos que",
    "he de": "tengo que",
}

# Paraphrase frames; ``{q}`` is the whole question, ``{k}`` the keyword core.
_FRAMES = [
    "{q}",
    "{q}",  # filled with variations below
    "hola! {q_lower}",
    "una cosa: {q_lower}",
    "algú em pot dir {q_lower_no_mark}",
    "saps {q_lower_no_mark}",
    "em pots explicar {q_lower_no_mark}",
    "necessito saber {q_lower_no_mark}",
    "no em queda clar {q_lower_no_mark}",
    "displica {q_lower_no_mark}",
    "una pregunta: {q_lower}",
    "no sé {q_lower_no_mark}",
    "disculpa, {q_lower}",
    "buens, {q_lower}",
    "{q_lower_no_mark}??",
    "{q_lower_no_mark}",
    "{keywords}",
    "{keywords_lower}",
    "{mixed}",
    "{mixed_lower}",
    "{keywords_no_accents}",
    "{typo}",
    "{keywords_typo}",
    "quantes voltes m'he preguntat {q_lower_no_mark}",
    "com es fa això de {q_lower_no_mark}",
    "gente, {q_lower}",
    "va, {q_lower_no_mark}",
]


def _strip_accents(text: str) -> str:
    """Remove Catalan/Spanish diacritics (a common informal chat error)."""
    return text.translate(str.maketrans("àáèéíïòóúüçÀÁÈÉÍÏÒÓÚÜÇ", "aaeeiiouuuc" * 2))


def _typo(text: str, rng: random.Random) -> str:
    """Swap two adjacent letters in the longest word."""
    words = text.split()
    index = max(range(len(words)), key=lambda i: len(words[i]))
    word = words[index]
    if len(word) < 4:
        return text
    position = rng.randrange(1, len(word) - 2)
    words[index] = (
        word[:position] + word[position + 1] + word[position] + word[position + 2 :]
    )
    return " ".join(words)


def _keywords(text: str) -> str:
    """Keep the informative words, dropping question particles."""
    stop = {
        "a",
        "al",
        "als",
        "de",
        "del",
        "dels",
        "el",
        "els",
        "la",
        "les",
        "un",
        "una",
        "i",
        "o",
        "es",
        "és",
        "ha",
        "han",
        "hi",
        "cal",
        "per",
        "per a",
        "s'",
        "l'",
        "d'",
        "n'",
        "em",
        "et",
        "ens",
        "us",
        "se",
        "que",
        "què",
        "com",
        "quan",
        "on",
        "quant",
        "quina",
        "qui",
        "perquè",
        "si",
        "pot",
        "he",
        "hem",
        "fa",
        "fer",
        "the",
    }
    keep = [
        word
        for word in re.findall(r"[\w'·]+", text)
        if word.casefold() not in stop and len(word) > 2
    ]
    return " ".join(keep[:4]) if keep else text


def _mixed(text: str) -> str:
    """Apply the Spanish function-word swap table."""
    lowered = text
    for cat, es in _MIXED.items():
        lowered = re.sub(rf"\b{re.escape(cat)}\b", es, lowered, flags=re.IGNORECASE)
    return lowered


def _question_for(anchor: str, seed_items: list[dict]) -> str:
    for item in seed_items:
        if item.get("source_anchor") == anchor:
            question = item.get("question") or ""
            return question.rstrip("?").strip()
    raise SystemExit(f"anchor {anchor} is not in the seed knowledge base")  # noqa: TRY003


def main() -> None:
    """Write evals/retrieval_synthetic.yaml so both files total 500 cases."""
    rng = random.Random(SEED)
    gold = yaml.safe_load(
        (ROOT / "evals" / "retrieval.yaml").read_text(encoding="utf-8")
    )
    seed_items = json.loads(
        (ROOT / "data" / "seed" / "qa.json").read_text(encoding="utf-8")
    )
    classifier_texts: set[str] = set()
    for name in ("train.jsonl", "test.jsonl"):
        path = ROOT / "data" / "classifier" / name
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    classifier_texts.add(
                        " ".join(json.loads(line)["text"].casefold().split())
                    )
    anchors = sorted(
        {case["expected_anchor"] for case in gold}
        | {a for case in gold for a in case.get("also_accepts", [])}
    )
    existing_queries = {
        " ".join(str(case["query"]).casefold().split()) for case in gold
    }
    used: set[str] = set(existing_queries) | classifier_texts

    generated: list[dict] = []
    # Round-robin anchors until the target count is reached.
    template_count = len(_FRAMES)
    variants_per_anchor: dict[str, list[str]] = {}
    for anchor in anchors:
        question = _question_for(anchor, seed_items)
        variants: list[str] = []
        for frame in _FRAMES:
            variants.append(
                frame.format(
                    q=question,
                    q_lower=question[0].lower() + question[1:],
                    q_lower_no_mark=question[0].lower() + question[1:],
                    keywords=_keywords(question),
                    keywords_lower=_keywords(question).lower(),
                    keywords_no_accents=_strip_accents(_keywords(question)),
                    mixed=_mixed(question),
                    mixed_lower=_mixed(question).lower(),
                    keywords_typo=_typo(_keywords(question), rng),
                    typo=_typo(question, rng),
                )
            )
        variants_per_anchor[anchor] = variants
    del template_count

    round_index = 0
    while len(generated) < TOTAL - len(gold) and round_index < len(_FRAMES):
        for anchor in anchors:
            if len(generated) >= TOTAL - len(gold):
                break
            query = variants_per_anchor[anchor][round_index]
            normalized = " ".join(query.casefold().split())
            if not normalized or normalized in used:
                continue
            used.add(normalized)
            generated.append(
                {"query": query, "expected_anchor": anchor, "source": "synthetic"}
            )
        round_index += 1
    if len(gold) + len(generated) != TOTAL:
        raise SystemExit(  # noqa: TRY003
            f"only produced {len(gold) + len(generated)} of {TOTAL} queries"
        )
    rng.shuffle(generated)
    out = ROOT / "evals" / "retrieval_synthetic.yaml"
    lines = [
        "# SPDX-License-Identifier: MIT",
        "# Synthetic retrieval-eval queries generated from existing seed anchors",
        "# by scripts/generate_retrieval_eval.py (deterministic seed).",
        "# No new anchors or facts; Catalan, Spanish, mixed, typos, keywords.",
    ]
    for case in generated:
        query = case["query"].replace('"', '\\"')
        lines.append(
            f'- query: "{query}"\n  expected_anchor: {case["expected_anchor"]}\n'
            "  source: synthetic"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{out}: {len(generated)} synthetic queries (total {TOTAL})")


if __name__ == "__main__":
    main()
