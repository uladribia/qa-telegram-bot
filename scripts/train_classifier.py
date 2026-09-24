# SPDX-License-Identifier: MIT
"""Train the linear intent-classifier head locally and export model.json.

Pipeline: dataset texts -> Ollama embeddinggemma embeddings -> sklearn
multinomial logistic regression -> metrics on the fixed 500-case test split ->
JSON export. scikit-learn is a dev/training dependency only; the runtime uses
the exported coefficients with plain Python.
"""

import hashlib
import json
from pathlib import Path

import httpx
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "classifier"
MODEL_PATH = DATA_DIR / "model.json"
OLLAMA_URL = "http://127.0.0.1:11434/api/embed"
EMBEDDING_MODEL = "embeddinggemma"
BATCH = 64
SEED = 7


def load_split(name: str) -> tuple[list[str], list[str]]:
    """Read one JSONL split into parallel text and label lists."""
    cases = [
        json.loads(line)
        for line in (DATA_DIR / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [case["text"] for case in cases], [case["label"] for case in cases]


def embed(texts: list[str]) -> np.ndarray:
    """Embed texts with the local Ollama embeddinggemma model."""
    vectors: list[list[float]] = []
    client = httpx.Client(timeout=120.0)
    for start in range(0, len(texts), BATCH):
        batch = texts[start : start + BATCH]
        response = client.post(
            OLLAMA_URL, json={"model": EMBEDDING_MODEL, "input": batch}
        )
        response.raise_for_status()
        vectors.extend(response.json()["embeddings"])
        print(f"embedded {min(start + BATCH, len(texts))}/{len(texts)}")
    return np.asarray(vectors, dtype=np.float64)


def report(
    name: str, labels: list[str], predictions: np.ndarray, probabilities: np.ndarray
) -> None:
    """Print the classifier metrics for one evaluation split."""
    ordered = sorted(set(labels))
    print(f"\n[{name}] accuracy={accuracy_score(labels, predictions):.4f}")
    print(f"[{name}] macro F1={f1_score(labels, predictions, average='macro'):.4f}")
    print(
        f"[{name}] per-class P/R/F1:\n"
        + precision_recall_fscore_support(
            labels, predictions, labels=ordered, zero_division=0
        ).__repr__()
    )
    print(f"[{name}] confusion (rows=true {ordered}):")
    print(confusion_matrix(labels, predictions, labels=ordered))
    top2 = np.sort(probabilities, axis=1)[:, -2:]
    margins = top2[:, 1] - top2[:, 0]
    confident = (probabilities.max(axis=1) >= 0.60) & (margins >= 0.15)
    confident_labels = np.asarray(labels)[confident]
    confident_predictions = predictions[confident]
    if len(confident_labels):
        print(
            f"[{name}] coverage={confident.mean():.4f}"
            f" precision_among_confident="
            f"{(confident_labels == confident_predictions).mean():.4f}"
        )


def main() -> None:
    """Train, validate, and export the linear head."""
    train_texts, train_labels = load_split("train.jsonl")
    test_texts, test_labels = load_split("test.jsonl")
    train_vectors = embed(train_texts)
    test_vectors = embed(test_texts)
    # L2-normalize so the head works on unit vectors, like retrieval does.
    train_vectors /= np.linalg.norm(train_vectors, axis=1, keepdims=True)
    test_vectors /= np.linalg.norm(test_vectors, axis=1, keepdims=True)
    # C selected by 5-fold CV on the TRAIN split only (macro F1); never on
    # the test split, per the plan's no-tuning rule.
    model = LogisticRegression(max_iter=2000, C=16, random_state=SEED)
    model.fit(train_vectors, train_labels)
    predictions = model.predict(test_vectors)
    probabilities = model.predict_proba(test_vectors)
    ordered = list(model.classes_)
    print("classes:", ordered)
    report("test", test_labels, predictions, probabilities)
    MODEL_PATH.write_text(
        json.dumps(
            {
                "embedding_model": EMBEDDING_MODEL,
                "labels": ordered,
                "coef": model.coef_.tolist(),
                "intercept": model.intercept_.tolist(),
                "train_sha256": hashlib.sha256(
                    (DATA_DIR / "train.jsonl").read_bytes()
                ).hexdigest(),
                "created_from_cases": len(train_labels),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nexported {MODEL_PATH}")


if __name__ == "__main__":
    main()
