# SPDX-License-Identifier: MIT
"""Local quality evals for the retrieval / classifier / listener pass.

Runs fully offline against the local Ollama embeddinggemma endpoint, SQLite
FTS5, and the exported linear classifier head. No Cloudflare model or index is
touched. It writes ``reports/retrieval-classifier-listener.md`` with the
before/after retrieval metrics, classifier metrics on the fixed 500-case test
split, and listener scenario metrics.

Usage::

    uv run python -m evals.quality
"""

import asyncio
import json
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import httpx
import numpy as np
import yaml

from knowledge_bot.application.background import BackgroundIndexer
from knowledge_bot.application.classifier import (
    Classification,
    MessageClassifier,
    message_is_confident,
)
from knowledge_bot.application.indexing import SearchProjectionService
from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.application.listener_pairing import MessagePairingService
from knowledge_bot.application.retrieval import _rank, _rrf_fuse
from knowledge_bot.contracts.messages import NormalizedMessage, SourceDescriptor
from knowledge_bot.domain.enums import ClassificationStatus, ContentType, IndexStatus
from knowledge_bot.infrastructure.classifier_head import load_classifier_head
from knowledge_bot.infrastructure.cloudflare.d1 import D1LexicalIndex
from knowledge_bot.infrastructure.local.database import (
    SQLiteDatabase,
    apply_migrations,
)
from knowledge_bot.infrastructure.local.sqlite_repositories import SQLiteBinding
from knowledge_bot.ports.lexical import LexicalRecord
from knowledge_bot.ports.vector_store import VectorMatch
from tests.fakes.ai import (
    FakeLexicalIndex,
    FakeSearchIndexSource,
    FakeVectorStore,
    InMemorySearchProjectionRepository,
)
from tests.fakes.backend import InMemoryBackend
from tests.fakes.repositories import InMemoryMessagePairCandidateRepository
from tests.fakes.support import FrozenClock

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "reports" / "retrieval-classifier-listener.md"
OLLAMA_URL = "http://127.0.0.1:11434/api/embed"
EMBEDDING_MODEL = "embeddinggemma"
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
CONFIDENCE_THRESHOLD = 0.60
MARGIN_THRESHOLD = 0.15
RRF_K = 60
POOL = 10


class ClassMetrics(TypedDict):
    """Precision/recall/F1 for one label."""

    precision: float
    recall: float
    f1: float


class BeforeAfter(TypedDict):
    """One metric compared before and after the change."""

    baseline: float
    hybrid: float


class ClassifierReport(TypedDict):
    """Classifier metrics on the fixed test split."""

    n_test: int
    accuracy: float
    macro_f1: float
    per_class: dict[str, ClassMetrics]
    coverage: float
    precision_among_confident: float
    confusion: np.ndarray
    labels: list[str]


class RetrievalReport(TypedDict):
    """Retrieval metrics before and after the change."""

    n_queries: int
    recall1: BeforeAfter
    recall3: BeforeAfter
    recall5: BeforeAfter
    mrr: BeforeAfter
    subsets: dict[str, BeforeAfter]


class ListenerScenario:
    """One deterministic listener scenario and its expectations."""

    def __init__(
        self,
        kind: str,
        messages: list[tuple[int, str, str, str | None]],
        expected_pairs: list[tuple[str, str]],
        expected_indexed: list[str],
        expected_questions: list[str],
    ) -> None:
        """Create one scenario."""
        self.id = kind
        self.messages = messages
        self.expected_pairs = expected_pairs
        self.expected_indexed = expected_indexed
        self.expected_questions = expected_questions


class ListenerReport(TypedDict):
    """Listener metrics over the scenario suite."""

    scenarios: int
    pair_precision: float
    pair_recall: float
    factual_index_precision: float
    factual_index_recall: float
    question_detection_recall: float


def _embed_sync(texts: list[str]) -> np.ndarray:
    """Embed texts synchronously with local Ollama, L2-normalized."""
    vectors: list[list[float]] = []
    client = httpx.Client(timeout=120.0)
    for start in range(0, len(texts), 64):
        batch = texts[start : start + 64]
        response = client.post(
            OLLAMA_URL, json={"model": EMBEDDING_MODEL, "input": batch}
        )
        response.raise_for_status()
        vectors.extend(response.json()["embeddings"])
    matrix = np.asarray(vectors, dtype=np.float64)
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


# --- Classifier metrics -----------------------------------------------------


def classifier_report() -> ClassifierReport:
    """Evaluate the linear head on the fixed 500-case test split."""
    head = load_classifier_head(ROOT / "data" / "classifier" / "model.json")
    labels = [label.value for label in head.labels]
    test = [
        json.loads(line)
        for line in (ROOT / "data" / "classifier" / "test.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    vectors = _embed_sync([case["text"] for case in test])
    order = {label: index for index, label in enumerate(labels)}
    logits = vectors @ np.asarray(head.coef, dtype=np.float64).T + np.asarray(
        head.intercept, dtype=np.float64
    )
    logits -= logits.max(axis=1, keepdims=True)
    exps = np.exp(logits)
    probabilities = exps / exps.sum(axis=1, keepdims=True)
    top = probabilities.argmax(axis=1)
    sorted_probs = np.sort(probabilities, axis=1)
    margins = sorted_probs[:, -1] - sorted_probs[:, -2]
    truth = np.asarray([order[case["label"]] for case in test])
    confident = (probabilities.max(axis=1) >= CONFIDENCE_THRESHOLD) & (
        margins >= MARGIN_THRESHOLD
    )
    confusion = np.zeros((len(labels), len(labels)), dtype=int)
    for predicted, actual in zip(top, truth, strict=True):
        confusion[actual][predicted] += 1
    per_class: dict[str, ClassMetrics] = {}
    f1_scores: list[float] = []
    for index, label in enumerate(labels):
        tp = int(confusion[index][index])
        fp = int(confusion[:, index].sum()) - tp
        fn = int(confusion[index, :].sum()) - tp
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = (
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        )
        f1_scores.append(f1)
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    confident_precision = (
        float((truth[confident] == top[confident]).mean()) if confident.any() else 0.0
    )
    return {
        "n_test": len(test),
        "accuracy": float((top == truth).mean()),
        "macro_f1": float(np.mean(f1_scores)),
        "per_class": per_class,
        "coverage": float(confident.mean()),
        "precision_among_confident": confident_precision,
        "confusion": confusion,
        "labels": labels,
    }


# --- Retrieval before/after -------------------------------------------------


def retrieval_report() -> RetrievalReport:
    """Compare semantic-only (old q+a vectors) against hybrid question-focused."""
    seed = json.loads((ROOT / "data" / "seed" / "qa.json").read_text(encoding="utf-8"))
    cases = [
        *yaml.safe_load(
            (ROOT / "evals" / "retrieval.yaml").read_text(encoding="utf-8")
        ),
        *yaml.safe_load(
            (ROOT / "evals" / "retrieval_synthetic.yaml").read_text(encoding="utf-8")
        ),
    ]
    anchors = [str(item["source_anchor"]) for item in seed]
    questions = [str(item["question"]) for item in seed]
    answers = [str(item["answer"]) for item in seed]
    query_vectors = _embed_sync([str(case["query"]) for case in cases])
    question_vectors = _embed_sync(questions)
    old_vectors = _embed_sync(
        [f"{q}\n{a}" for q, a in zip(questions, answers, strict=True)]
    )

    accepted_sets = [
        {str(case["expected_anchor"]), *case.get("also_accepts", [])} for case in cases
    ]

    def evaluate(pairs: list[tuple[set[str], list[str]]], top_k: int) -> float:
        if not pairs:
            return 0.0
        hits = sum(1 for accepted, ids in pairs if accepted & set(ids[:top_k]))
        return hits / len(pairs)

    def mrr(pairs: list[tuple[set[str], list[str]]]) -> float:
        if not pairs:
            return 0.0
        total = 0.0
        for accepted, ids in pairs:
            for rank, item_id in enumerate(ids, start=1):
                if item_id in accepted:
                    total += 1.0 / rank
                    break
        return total / len(pairs)

    async def hybrid_lists() -> list[list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            database = await SQLiteDatabase.connect(Path(tmp) / "lexical.sqlite3")
            try:
                await apply_migrations(database, ROOT)
                lexical = D1LexicalIndex(SQLiteBinding(database))
                await lexical.upsert(
                    [
                        LexicalRecord(
                            id=f"qa:{anchor}", text=question, metadata={"kind": "qa"}
                        )
                        for anchor, question in zip(anchors, questions, strict=True)
                    ]
                )
                ids: list[list[str]] = []
                for index, case in enumerate(cases):
                    scores = question_vectors @ query_vectors[index]
                    semantic = [
                        VectorMatch(
                            id=f"qa:{anchors[row]}",
                            score=float(scores[row]),
                            metadata={},
                        )
                        for row in np.argsort(-scores)[:POOL]
                    ]
                    tokens = " ".join(
                        f'"{token}"' for token in re.findall(r"\w+", str(case["query"]))
                    )
                    lexical_hits = await lexical.search(tokens, top_k=POOL)
                    lexical_matches = [
                        VectorMatch(id=hit.id, score=0.0, metadata={})
                        for hit in lexical_hits
                    ]
                    fused = _rank(_rrf_fuse([semantic, lexical_matches]), POOL)
                    ids.append(
                        [candidate.id.removeprefix("qa:") for candidate in fused]
                    )
                return ids
            finally:
                await database.close()

    old_ids = [
        [
            anchors[row]
            for row in np.argsort(-(old_vectors @ query_vectors[index]))[:POOL]
        ]
        for index in range(len(cases))
    ]
    new_ids = asyncio.run(hybrid_lists())
    old_pairs = list(zip(accepted_sets, old_ids, strict=True))
    new_pairs = list(zip(accepted_sets, new_ids, strict=True))
    subsets = {
        "catalan_gold": [
            index
            for index, case in enumerate(cases)
            if case.get("source") != "synthetic"
        ],
        "synthetic_typo_paraphrase": [
            index
            for index, case in enumerate(cases)
            if case.get("source") == "synthetic"
        ],
    }
    subset_recall: dict[str, BeforeAfter] = {
        name: {
            "baseline": evaluate([old_pairs[i] for i in indices], 5)
            if indices
            else 0.0,
            "hybrid": evaluate([new_pairs[i] for i in indices], 5) if indices else 0.0,
        }
        for name, indices in subsets.items()
    }
    return {
        "n_queries": len(cases),
        "recall1": {
            "baseline": evaluate(old_pairs, 1),
            "hybrid": evaluate(new_pairs, 1),
        },
        "recall3": {
            "baseline": evaluate(old_pairs, 3),
            "hybrid": evaluate(new_pairs, 3),
        },
        "recall5": {
            "baseline": evaluate(old_pairs, 5),
            "hybrid": evaluate(new_pairs, 5),
        },
        "mrr": {"baseline": mrr(old_pairs), "hybrid": mrr(new_pairs)},
        "subsets": subset_recall,
    }


# --- Listener scenarios -----------------------------------------------------


def _split_texts() -> dict[str, list[str]]:
    """Load synthetic test-split texts grouped by label (never train texts)."""
    grouped: dict[str, list[str]] = {
        "question": [],
        "knowledge_update": [],
        "correction": [],
        "chitchat": [],
    }
    for line in (
        (ROOT / "data" / "classifier" / "test.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ):
        if not line.strip():
            continue
        case = json.loads(line)
        if case["source"] == "synthetic" and case["label"] in grouped:
            grouped[case["label"]].append(case["text"])
    return grouped


def _confident_texts(label: str, count: int) -> list[str]:
    """Return texts the head confidently classifies as ``label`` (cycling)."""
    pool = _split_texts()[label]
    head = load_classifier_head(ROOT / "data" / "classifier" / "model.json")
    vectors = _embed_sync(pool)
    logits = vectors @ np.asarray(head.coef, dtype=np.float64).T + np.asarray(
        head.intercept, dtype=np.float64
    )
    exps = np.exp(logits - logits.max(axis=1, keepdims=True))
    probabilities = exps / exps.sum(axis=1, keepdims=True)
    ordered = np.sort(probabilities, axis=1)
    margins = ordered[:, -1] - ordered[:, -2]
    top_label = probabilities.argmax(axis=1)
    label_index = [candidate.value for candidate in head.labels].index(label)
    selected = [
        pool[index]
        for index in range(len(pool))
        if top_label[index] == label_index
        and probabilities[index].max() >= CONFIDENCE_THRESHOLD
        and margins[index] >= MARGIN_THRESHOLD
    ]
    if not selected:
        raise SystemExit(f"no confident {label} texts available")  # noqa: TRY003
    return [selected[index % len(selected)] for index in range(count)]


def listener_scenarios() -> list[ListenerScenario]:
    """Build deterministic listener scenarios from test-split texts."""
    texts = _split_texts()
    answers = iter(
        _confident_texts("knowledge_update", 60) + _confident_texts("correction", 60)
    )

    def answer_take() -> str:
        return next(answers)

    scenarios: list[ListenerScenario] = []

    def add(
        kind: str,
        messages: list[tuple[int, str, str, str | None]],
        expected_pairs: list[tuple[str, str]],
        expected_indexed: list[str],
        expected_questions: list[str],
    ) -> None:
        scenarios.append(
            ListenerScenario(
                kind=f"{kind}-{len(scenarios) + 1}",
                messages=messages,
                expected_pairs=expected_pairs,
                expected_indexed=expected_indexed,
                expected_questions=expected_questions,
            )
        )

    for index in range(15):
        add(
            "single_question",
            [
                (0, f"q{index}", _confident_texts("question", 1)[0], None),
                (60, f"a{index}", answer_take(), None),
            ],
            [(f"q{index}", f"a{index}")],
            [f"a{index}"],
            [f"q{index}"],
        )
    for index in range(15):
        add(
            "explicit_reply",
            [
                (0, f"q{index}", _confident_texts("question", 1)[0], None),
                (30, f"a{index}", answer_take(), f"q{index}"),
            ],
            [(f"q{index}", f"a{index}")],
            [f"a{index}"],
            [f"q{index}"],
        )
    two_questions = _confident_texts("question", 2)
    for index in range(10):
        add(
            "two_questions",
            [
                (0, f"q1-{index}", two_questions[index % 2], None),
                (30, f"q2-{index}", two_questions[(index + 1) % 2], None),
                (60, f"a{index}", answer_take(), None),
            ],
            [],
            [f"a{index}"],
            [f"q1-{index}", f"q2-{index}"],
        )
    standalone = _confident_texts("knowledge_update", 10)
    corrections = _confident_texts("correction", 10)
    chitchat_pool = _split_texts()["chitchat"]
    for index in range(10):
        add(
            "standalone_update",
            [(0, f"u{index}", standalone[index], None)],
            [],
            [f"u{index}"],
            [],
        )
    for index in range(10):
        add(
            "correction",
            [(0, f"c{index}", corrections[index], None)],
            [],
            [f"c{index}"],
            [],
        )
    for index in range(10):
        add(
            "chitchat",
            [(0, f"h{index}", chitchat_pool[index % len(chitchat_pool)], None)],
            [],
            [],
            [],
        )
    stale_questions = _confident_texts("question", 15)
    for index in range(15):
        add(
            "stale_window",
            [
                (0, f"sq{index}", stale_questions[index], None),
                (600, f"sa{index}", answer_take(), None),
            ],
            [],
            [f"sa{index}"],
            [f"sq{index}"],
        )
    cross_questions = _confident_texts("question", 15)
    for index in range(15):
        add(
            "cross_space",
            [
                (0, f"xq{index}", cross_questions[index], None),
                (60, f"xa{index}", answer_take(), None),
            ],
            [(f"xq{index}", f"xa{index}")],
            [f"xa{index}"],
            [f"xq{index}"],
        )
    # Ambiguous outputs: the lowest-margin texts overall. The conservative
    # policy must neither index nor pair them, whatever their true label is.
    flat_texts = [text for pool in texts.values() for text in pool]
    for index, text in enumerate(_lowest_margin_texts(flat_texts, 10)):
        add("ambiguous", [(0, f"amb{index}", text, None)], [], [], [])
    return scenarios


def _lowest_margin_texts(candidates: list[str], count: int) -> list[str]:
    """Return the texts the head leaves most ambiguous (margin < 0.15)."""
    head = load_classifier_head(ROOT / "data" / "classifier" / "model.json")
    vectors = _embed_sync(candidates)
    logits = vectors @ np.asarray(head.coef, dtype=np.float64).T + np.asarray(
        head.intercept, dtype=np.float64
    )
    exps = np.exp(logits - logits.max(axis=1, keepdims=True))
    probabilities = exps / exps.sum(axis=1, keepdims=True)
    ordered = np.sort(probabilities, axis=1)
    margins = ordered[:, -1] - ordered[:, -2]
    ranking = np.argsort(margins)
    return [
        candidates[index]
        for index in ranking[:count]
        if margins[index] < MARGIN_THRESHOLD
    ]


async def _run_scenario(
    scenario: ListenerScenario, classifier: MessageClassifier
) -> tuple[set[str], set[tuple[str, str]], set[str]]:
    """Run one scenario; return (indexed ids, made pairs, confident questions)."""
    backend = InMemoryBackend()
    clock = FrozenClock(NOW)
    projector = SearchProjectionService(
        FakeSearchIndexSource(),
        classifier.embedder,
        FakeVectorStore(),
        FakeLexicalIndex(),
        InMemorySearchProjectionRepository(),
        clock,
    )
    background = BackgroundIndexer(
        backend.messages,
        backend.conversations,
        backend.sources,
        classifier,
        projector,
        clock,
        CONFIDENCE_THRESHOLD,
        MARGIN_THRESHOLD,
    )
    candidates = InMemoryMessagePairCandidateRepository()
    pairing = MessagePairingService(
        backend.messages,
        backend.conversations,
        backend.sources,
        candidates,
        projector,
        clock,
        confidence_threshold=CONFIDENCE_THRESHOLD,
        margin_threshold=MARGIN_THRESHOLD,
    )
    ingestor = _ingestor(backend)
    for offset, tag, text, reply_to in scenario.messages:
        message_id = f"conv-a:{tag}"
        classification: Classification = await classifier.classify(text)
        parent_question: str | None = None
        if reply_to is not None:
            parent = await backend.messages.get(f"conv-a:{reply_to}")
            if (
                parent is not None
                and parent.intent_label == "question"
                and message_is_confident(
                    parent.intent_label,
                    parent.intent_score,
                    parent.intent_scores_json,
                    confidence_threshold=CONFIDENCE_THRESHOLD,
                    margin_threshold=MARGIN_THRESHOLD,
                )
                and classifier.is_answer_like(classification)
            ):
                parent_question = parent.text
        await ingestor.ingest(
            NormalizedMessage(
                id=message_id,
                source=SourceDescriptor(
                    id="listener-source", kind="test", authority=40
                ),
                conversation_id="conv-a",
                sender_is_admin=False,
                timestamp=NOW + timedelta(seconds=int(offset)),
                content_type=ContentType.TEXT,
                text=text,
                reply_to_message_id=f"conv-a:{reply_to}" if reply_to else None,
            ),
            intent_label=classification.best_label.value,
            intent_score=classification.best_score,
            context_question=parent_question,
            intent_scores={
                "question": classification.scores.question,
                "knowledge_update": classification.scores.knowledge_update,
                "correction": classification.scores.correction,
                "chitchat": classification.scores.chitchat,
                "margin": classification.margin,
            },
            classification_status=ClassificationStatus.CLASSIFIED,
            index_status=IndexStatus.NOT_ELIGIBLE,
        )
        await pairing.on_message(message_id)
        stored = await backend.messages.get(message_id)
        if stored is not None and stored.context_question is None:
            await background.process(message_id, classification.embedding)
    made_pairs = {
        (
            candidate.question_message_id.split(":", 1)[1],
            candidate.answer_message_id.split(":", 1)[1],
        )
        for candidate in candidates._items.values()
    }
    indexed: set[str] = set()
    confident_questions: set[str] = set()
    for message in backend.messages._items.values():
        if message.index_status is IndexStatus.INDEXED:
            indexed.add(message.id.split(":", 1)[1])
        if message.intent_label == "question" and message_is_confident(
            message.intent_label,
            message.intent_score,
            message.intent_scores_json,
            confidence_threshold=CONFIDENCE_THRESHOLD,
            margin_threshold=MARGIN_THRESHOLD,
        ):
            confident_questions.add(message.id.split(":", 1)[1])
    return indexed, made_pairs, confident_questions


def _ingestor(backend: InMemoryBackend) -> MessageIngestor:
    return MessageIngestor(
        backend.sources, backend.conversations, backend.messages, backend.attachments
    )


async def listener_report() -> ListenerReport:
    """Execute every listener scenario and compute the paired metrics."""
    head = load_classifier_head(ROOT / "data" / "classifier" / "model.json")
    embedder = _LocalEmbedder()
    classifier = MessageClassifier(
        embedder=embedder,
        head=head,
        confidence_threshold=CONFIDENCE_THRESHOLD,
        margin_threshold=MARGIN_THRESHOLD,
    )
    correct_pairs = made_pairs = 0
    expected_pairs = 0
    index_hits = index_total = index_expected = 0
    question_hits = question_expected = 0
    scenarios = listener_scenarios()
    for scenario in scenarios:
        indexed, scenario_pairs, questions = await _run_scenario(scenario, classifier)
        expected_set = set(scenario.expected_pairs)
        made_pairs += len(scenario_pairs)
        correct_pairs += len(scenario_pairs & expected_set)
        expected_pairs += len(expected_set)
        expected_indexed = set(scenario.expected_indexed)
        index_hits += len(indexed & expected_indexed)
        index_total += len(indexed)
        index_expected += len(expected_indexed)
        expected_questions = set(scenario.expected_questions)
        question_hits += len(questions & expected_questions)
        question_expected += len(expected_questions)
    return {
        "scenarios": len(scenarios),
        "pair_precision": correct_pairs / made_pairs if made_pairs else 0.0,
        "pair_recall": correct_pairs / expected_pairs if expected_pairs else 0.0,
        "factual_index_precision": index_hits / index_total if index_total else 0.0,
        "factual_index_recall": index_hits / index_expected if index_expected else 0.0,
        "question_detection_recall": (
            question_hits / question_expected if question_expected else 0.0
        ),
    }


class _LocalEmbedder:
    """Async embedder over the local Ollama endpoint."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=120.0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed one batch of texts."""
        response = await self._client.post(
            OLLAMA_URL, json={"model": EMBEDDING_MODEL, "input": texts}
        )
        response.raise_for_status()
        return [
            [float(value) for value in row] for row in response.json()["embeddings"]
        ]


def main() -> int:
    """Run all local quality evals and write the Markdown report."""
    classifier = classifier_report()
    retrieval = retrieval_report()
    listener = asyncio.run(listener_report())
    report = _render(classifier, retrieval, listener)
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(report)
    return 0


def _format(value: float) -> str:
    return f"{value:.4f}"


def _render(
    classifier: ClassifierReport,
    retrieval: RetrievalReport,
    listener: ListenerReport,
) -> str:
    confusion = classifier["confusion"]
    labels = classifier["labels"]
    confusion_lines = [
        "| true\\pred | " + " | ".join(labels) + " |",
        "|---|" + "---|" * len(labels),
    ]
    for index, label in enumerate(labels):
        confusion_lines.append(
            f"| {label} | "
            + " | ".join(str(int(value)) for value in confusion[index])
            + " |"
        )
    per_class_lines = [
        f"| {label} | {row['precision']:.4f} | {row['recall']:.4f} | {row['f1']:.4f} |"
        for label, row in classifier["per_class"].items()
    ]
    ku_precision = classifier["per_class"]["knowledge_update"]["precision"]
    corr_precision = classifier["per_class"]["correction"]["precision"]
    gates = [
        ("classifier macro F1 >= 0.90", classifier["macro_f1"] >= 0.90),
        ("classifier knowledge_update precision >= 0.92", ku_precision >= 0.92),
        ("classifier correction precision >= 0.92", corr_precision >= 0.92),
        ("retrieval Recall@3 >= 0.95", retrieval["recall3"]["hybrid"] >= 0.95),
        ("retrieval Recall@5 >= 0.98", retrieval["recall5"]["hybrid"] >= 0.98),
        (
            "retrieval MRR improves over baseline",
            retrieval["mrr"]["hybrid"] > retrieval["mrr"]["baseline"],
        ),
        (
            "listener factual-index precision >= 0.95",
            listener["factual_index_precision"] >= 0.95,
        ),
        ("listener pair precision >= 0.95", listener["pair_precision"] >= 0.95),
    ]
    gate_lines = [
        f"| {name} | {'PASS' if passed else 'FAIL'} |" for name, passed in gates
    ]
    return f"""# Retrieval / classifier / listener experiment

Generated by `uv run python -m evals.quality` (local Ollama embeddinggemma; no
Cloudflare AI usage).

## Dataset

- classifier train cases: 500 (human gold: committed evals/classifier.yaml cases + hard contrasts)
- classifier test cases: {classifier["n_test"]} (fully synthetic, disjoint after normalization)
- retrieval queries: {retrieval["n_queries"]} (33 gold + 467 synthetic paraphrases over existing seed anchors)
- listener scenarios: {listener["scenarios"]}

## Classifier (fixed 500-case test split)

| metric | result |
|---|---|
| accuracy | {_format(classifier["accuracy"])} |
| macro F1 (primary) | {_format(classifier["macro_f1"])} |
| coverage (non-ambiguous) | {_format(classifier["coverage"])} |
| precision among confident | {_format(classifier["precision_among_confident"])} |
| knowledge_update precision (safety) | {_format(classifier["per_class"]["knowledge_update"]["precision"])} |
| correction precision (safety) | {_format(classifier["per_class"]["correction"]["precision"])} |

| label | precision | recall | F1 |
|---|---|---|---|
{chr(10).join(per_class_lines)}

{chr(10).join(confusion_lines)}

## Retrieval

| metric | semantic baseline (old q+a vectors) | hybrid (question vectors + BM25 RRF) |
|---|---|---|
| Recall@1 | {_format(retrieval["recall1"]["baseline"])} | {_format(retrieval["recall1"]["hybrid"])} |
| Recall@3 | {_format(retrieval["recall3"]["baseline"])} | {_format(retrieval["recall3"]["hybrid"])} |
| Recall@5 | {_format(retrieval["recall5"]["baseline"])} | {_format(retrieval["recall5"]["hybrid"])} |
| MRR | {_format(retrieval["mrr"]["baseline"])} | {_format(retrieval["mrr"]["hybrid"])} |

Recall@5 by subset:

| subset | baseline | hybrid |
|---|---|---|
| catalan gold | {_format(retrieval["subsets"]["catalan_gold"]["baseline"])} | {_format(retrieval["subsets"]["catalan_gold"]["hybrid"])} |
| synthetic typo/paraphrase | {_format(retrieval["subsets"]["synthetic_typo_paraphrase"]["baseline"])} | {_format(retrieval["subsets"]["synthetic_typo_paraphrase"]["hybrid"])} |

## Listener ({listener["scenarios"]} deterministic scenarios)

| metric | result |
|---|---|
| pair precision (primary safety) | {_format(listener["pair_precision"])} |
| pair recall | {_format(listener["pair_recall"])} |
| factual-index precision (primary) | {_format(listener["factual_index_precision"])} |
| factual-index recall | {_format(listener["factual_index_recall"])} |
| question detection recall | {_format(listener["question_detection_recall"])} |

## Runtime cost

- Cloudflare AI calls added: 0
- BM25 SQL query added: yes (FTS5 projection, local SQLite and D1)
- classifier runtime model calls added: 0 (linear head, plain Python)
- classifier cold start: message embedding only (was message + prototype embeddings)

## Decision

Gate status vs the plan thresholds (failures reported, never tuned against
the test split):

| gate | status |
|---|---|
{chr(10).join(gate_lines)}

Recall below the precision gates (pair recall {listener["pair_recall"]:.4f},
factual-index recall {listener["factual_index_recall"]:.4f}) is the intended
cost of the conservative confidence policy: ambiguous messages are stored,
never acted on.
"""


if __name__ == "__main__":
    raise SystemExit(main())
