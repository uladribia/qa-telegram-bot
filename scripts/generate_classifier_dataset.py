# SPDX-License-Identifier: MIT
"""Generate the deterministic 500 + 500 intent-classifier dataset.

Sources, in order:

1. existing human JSONL splits under ``data/classifier/`` (never rewritten);
2. the committed ``evals/classifier.yaml`` gold cases (single-label ones);
3. the mandatory hard contrast pairs from the plan;
4. deterministic synthetic examples from linguistic templates.

Output: ``data/classifier/train.jsonl`` and ``data/classifier/test.jsonl``,
each with exactly 500 cases, disjoint after normalization.
"""

import json
import random
import re
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "classifier"
SEED = 20260101
TARGETS = {"question": 150, "knowledge_update": 125, "correction": 100, "chitchat": 125}
TOTAL = 500

DAYS_CA = [
    "dilluns",
    "dimarts",
    "dimecres",
    "dijous",
    "divendres",
    "dissabte",
    "diumenge",
]
DAYS_ES = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
TIMES = [
    "a les 6",
    "a les 6:30",
    "a les 7",
    "a les 17:00",
    "a les 18:15",
    "a dos quarts de set",
    "al migdia",
    "a primera hora",
    "a les 5 de la tarda",
    "a les 9:15",
    "abans de les 8",
    "cap a les 10",
]
PLACES = [
    "al camp de dalt",
    "al pavelló",
    "al Pau Negre",
    "al camp 2",
    "a la pista de sota",
    "al camp nou",
]
TEAMS = [
    "els Minis",
    "els Prebenjams",
    "els Benjamins",
    "els Alevins",
    "els Cadets",
    "el Juvenil B",
]
EQUIPMENT = [
    "la samarreta",
    "els pantalons",
    "les espinilleres",
    "les botes",
    "l'equipació",
    "el xandall",
    "la pilota",
    "les mitjones",
]
PAYMENTS = [
    "la fitxa",
    "el pagament de la lliga",
    "els 20 euros",
    "la quota del mes",
    "l'inscripció",
    "els 15 euros del torneig",
]
TRANSPORT = [
    "l'autocar",
    "el bus de les 16:00",
    "els cotxes dels pares",
    "el minibus",
    "el transport del club",
]
MEDICAL = [
    "el certificat mèdic",
    "l'informe mèdic",
    "la revisió",
    "el radiograf",
    "la validació mèdica",
]
COACHES = ["el Jordi", "la Marta", "el Dani", "la Laura", "el delegat", "l'entrenador"]
EMOJIS = ["⚽", "👍", "🙏", "😮‍💨", "✅", "🙌", "🔥", "😅"]
TOPICS = [
    "l'entrenament",
    "el partit",
    "la lliga",
    "el torneig",
    "la concentració",
    "l'amistós",
]

QUESTION_TEMPLATES = [
    "A quina hora és {topic} {day}?",
    "{day} hi ha {topic}? A quina hora?",
    "On és {topic} {time}?",
    "Qui porta {equip} als partits?",
    "Quant costa {payment}?",
    "On es compra {equip}?",
    "S'ha de portar {medical} per jugar?",
    "Com s'arriba {place}? Hi ha {transport}?",
    "A qui li he de dir que vindré tard {day}?",
    "Fins quan es pot pagar {payment}?",
    "Quan caduca {medical}?",
    "{team} juguen {day}? On?",
    "Hi ha entrenament si plou {day}?",
    "Algú sap a quina hora és {topic} de {team}?",
    "On consulto les mides de {equip}?",
    "He d'enviar {medical} abans de {day}?",
    "Com demano {equip} nova?",
    "Qui passa la llista per {topic}?",
    "A quina hora acaba {topic} {time}?",
    "Es pot pagar {payment} per bizum?",
    "Hi ha {topic} durant les vacances?",
    "Quant falta per {topic} de {team}?",
    "On penja el calendari dels partits de {team}?",
    "Algui sap si {day} hi ha {topic}?",
    "Que a les 6 hi ha {topic} o era a les 7?",
]
UPDATE_TEMPLATES = [
    "Recordeu que {day} no hi ha {topic}.",
    "{topic} de {day} canvia a {time}.",
    "La reunió de pares serà {day} {time} a la sala gran.",
    "El partit de {day} es juga {place}.",
    "Han publicat els horaris nous al web del club.",
    "Es pot tornar a pagar {payment} fins divendres.",
    "Porteu {equip} de recanvi al partit de {day}.",
    "El {transport} surt a les 15:45 del club.",
    "{medical} es lliura al delegat abans de jugar.",
    "{team} entrenarà {day} {time}.",
    "Avui no hi ha {topic}: plou i el camp està mullat.",
    "S'ha cancel·lat {topic} del {day}.",
    "La samarreta de l'equipació nova val 22 euros.",
    "El termini per pagar {payment} acaba el dia 15.",
    "Demanen portar {equip} blanca {day}.",
    "Els partits de {team} comencen a les 10:30.",
    "Cal portar {equip} i aigua a tots els entrenaments.",
    "El club manda avís: cal renovar {medical} aquest mes.",
    "Ara {team} juga {place}.",
    "El preu de {payment} puja a 25 euros.",
    "Aquest {day} hi ha entrenament normal a {time}.",
    "Recordeu portar la bombolla d'aigua a {topic}.",
    "El proper {day} comencem 15 minuts tard.",
    "S'ha canviat el camp del partit: ara {place}.",
]
CORRECTION_TEMPLATES = [
    "No, finalment {topic} és {day}.",
    "Correcció: {topic} comença {time}, no {time}.",
    "M'he equivocat, el partit és {day} no {day}.",
    "El preu de {payment} són 20 euros, no 15.",
    "Perdó, havia dit {day} però és {day}.",
    "El camp ha tornat a canviar: no {place}, sinó {place}.",
    "No és {time}, és {time}.",
    "Corregeixo el que vaig dir: {team} juga {day}.",
    "El {transport} no surt a les 4, surt a les 4:15.",
    "Vaig dir malament el camp: és {place}.",
    "No cal portar {equip} {day}, ho he preguntat.",
    "Ups: la trobada és {day}, no avui.",
    "El termini per {payment} no és divendres, és dimarts.",
    "Correcció de l'hora: {time}.",
    "Em vaig confondre de grup: és {team}, no {team}.",
    "Ho he comprovat: {topic} es queda {time}.",
    "No, el pagament es fa pel web, no per transferència.",
    "Esborreu el que vaig escriure: el partit és {day}.",
    "L'hora de {topic} ha canviat altre cop: {time}.",
    "El partit no és {place}, és {place}.",
]
CHITCHAT_TEMPLATES = [
    "Gràcies {coach}!",
    "Perfecte, doncs ens veiem {day}.",
    "D'acord, cap problema.",
    "Genial 😂😂",
    "Ok, apuntat!",
    "Moltes gràcies per la info!",
    "Vinga, fins {day}!",
    "Jejeje quina Bestia",
    "Bon dia a tots! ☀️",
    "Feliç any nou a tothom! 🎉",
    "Uau, quina calor avui",
    "Enhorabona als {team}! 👏",
    "Ànims {team} avui!",
    "Jo ja hi som {place} 😅",
    "Quina sort la dels altres...",
    "Molt bona aquesta foto! 😍",
    "Doncs molt bé, tot seguit.",
    "Vinga nois, ànims!",
    "Ah, d'acord, gràcies.",
    "Vale, ja ho he vist.",
    "Perfecto, gracias!",
    "De res, home.",
    "Buenaaa, quin dia.",
    "Jo també ho penso.",
    "Sí, home, sí 😄",
    "Uff, quin dia tenim.",
    "Fins demà, compis!",
    "Molt bé! 👌",
    "Gràcies per avisar!",
    "Deu n'hi do, quina jam.",
    "Quin bon partit ahir! 👏",
]

HARD_CONTRASTS = [
    ("Demà a les sis.", "knowledge_update"),
    ("Demà a les sis?", "question"),
    ("Finalment és diumenge.", "knowledge_update"),
    ("No, finalment és diumenge.", "correction"),
    ("Perfecte, diumenge doncs.", "chitchat"),
    ("Diumenge?", "question"),
    ("Crec que era a les sis.", "chitchat"),
    ("Han canviat l'hora.", "knowledge_update"),
    ("T'has equivocat, han canviat l'hora.", "correction"),
]

ACCENTS = str.maketrans(
    "àáèéíïòóúüçÀÁÈÉÍÏÒÓÚÜÇ",
    "aaeeiiouuuc" * 2,
)


def _normalize(text: str) -> str:
    """Return the canonical normalized form used for deduplication."""
    return " ".join(text.casefold().split())


def _corrupt(text: str, rng: random.Random) -> str:
    """Apply one deterministic informal-text variation."""
    choice = rng.random()
    if choice < 0.15:
        return text.translate(ACCENTS)
    if choice < 0.30:
        stripped = text.rstrip("?.!¡")
        index = rng.randrange(1, max(2, len(stripped) - 1))
        return (
            stripped[:index]
            + stripped[index + 1]
            + stripped[index]
            + stripped[index + 2 :]
        )
    if choice < 0.45:
        return text.translate(ACCENTS).lower()
    if choice < 0.55:
        return f"{text} {rng.choice(EMOJIS)}"
    if choice < 0.62:
        return text.replace(" de ", " de ").replace(" ", "  ", 1).strip()
    return text


_POOLS = {
    "day": DAYS_CA + DAYS_ES,
    "esday": DAYS_ES,
    "time": TIMES,
    "place": PLACES,
    "team": TEAMS,
    "equip": EQUIPMENT,
    "payment": PAYMENTS,
    "transport": TRANSPORT,
    "medical": MEDICAL,
    "coach": COACHES,
    "topic": TOPICS,
}


def _fill(template: str, rng: random.Random) -> str:
    """Substitute every slot occurrence with a random, locally varied value."""
    last: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        values = _POOLS[key]
        for _ in range(8):
            value = rng.choice(values)
            if value != last.get(key):
                break
        last[key] = value
        return value

    return re.sub(r"\{(\w+)\}", replace, template)


def _load_human() -> list[dict[str, str]]:
    """Load pre-existing human JSONL splits and the YAML gold cases."""
    human: list[dict[str, str]] = []
    for name in ("train.jsonl", "test.jsonl"):
        path = OUT_DIR / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            if case.get("source") == "human":
                human.append(
                    {"text": case["text"], "label": case["label"], "source": "human"}
                )
    yaml_cases = yaml.safe_load(
        (ROOT / "evals" / "classifier.yaml").read_text(encoding="utf-8")
    )
    for case in yaml_cases:
        labels = case.get("labels") or []
        if len(labels) == 1:
            human.append({"text": case["text"], "label": labels[0], "source": "human"})
    return human


def _generate(
    label: str, count: int, seen: set[str], rng: random.Random
) -> list[dict[str, str]]:
    """Generate deterministic, unique synthetic cases for one label."""
    templates = {
        "question": QUESTION_TEMPLATES,
        "knowledge_update": UPDATE_TEMPLATES,
        "correction": CORRECTION_TEMPLATES,
        "chitchat": CHITCHAT_TEMPLATES,
    }[label]
    generated: list[dict[str, str]] = []
    attempts = 0
    while len(generated) < count and attempts < count * 200:
        attempts += 1
        text = _fill(rng.choice(templates), rng)
        if rng.random() < 0.35:
            text = _corrupt(text, rng)
        normalized = _normalize(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        generated.append({"text": text, "label": label, "source": "synthetic"})
    if len(generated) < count:
        raise SystemExit(f"could not generate {count} unique {label} cases")  # noqa: TRY003
    return generated


def main() -> None:
    """Build both splits with exact counts and write them."""
    rng = random.Random(SEED)
    human = _load_human()
    seen: set[str] = set()
    unique_human = []
    for case in human:
        key = _normalize(case["text"])
        if key in seen:
            continue
        seen.add(key)
        unique_human.append(case)
    train_human = [
        *unique_human,
        *[
            {"text": text, "label": label, "source": "human"}
            for text, label in HARD_CONTRASTS
            if _normalize(text) not in seen
        ],
    ]
    # Deduplicate again after hard contrasts (a contrast may mirror a gold case).
    final_train_human: list[dict[str, str]] = []
    seen_train: set[str] = set()
    for case in train_human:
        key = _normalize(case["text"])
        if key in seen_train:
            continue
        seen_train.add(key)
        final_train_human.append(case)

    train_counts = Counter(case["label"] for case in final_train_human)
    train = list(final_train_human)
    for label, target in TARGETS.items():
        needed = target - train_counts[label]
        if needed > 0:
            train.extend(_generate(label, needed, seen_train, rng))
    overflow = len(train) - TOTAL
    if overflow > 0:
        # Too many human cases for one split: keep the balance note honest.
        raise SystemExit(f"train has {len(train)} cases ({overflow} over {TOTAL})")  # noqa: TRY003

    # Test: pure synthetic, exact balance, disjoint from train.
    test: list[dict[str, str]] = []
    seen_test: set[str] = set(seen_train)
    for label, target in TARGETS.items():
        test.extend(_generate(label, target, seen_test, rng))
    assert len(test) == TOTAL

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, cases in (("train.jsonl", train), ("test.jsonl", test)):
        rng.shuffle(cases)
        path = OUT_DIR / name
        with path.open("w", encoding="utf-8") as handle:
            for case in cases:
                handle.write(json.dumps(case, ensure_ascii=False) + "\n")
        counts = Counter(case["label"] for case in cases)
        humans = sum(1 for case in cases if case["source"] == "human")
        print(f"{path}: {len(cases)} cases ({humans} human), {dict(counts)}")


if __name__ == "__main__":
    main()
