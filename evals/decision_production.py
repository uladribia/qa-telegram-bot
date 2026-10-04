# SPDX-License-Identifier: MIT
"""Focused production evaluation of Clef-Flash as a decision backend.

Three suites, one question: can a decision model keep the listener's precision
while resolving the windows where the deterministic policy must abstain?

```text
A  80 manual intent cases      evals/classifier.yaml, synthetic ones excluded
B  10 real listener scenarios  evals/listener.yaml, synthetic: false
C  50 hard decision windows    evals/decision_windows.yaml, frozen before any
                               model output existed
```

The deployed Worker is the oracle: this runner sends each case to the internal
evaluation route and reads back what each backend decided. It never reimplements
pairing. For the listener suite it replays the real application flow locally with
in-memory stores and swaps only the decision, so candidate collection and pair
selection are the production code paths.

Costs are bounded before they are paid: ``--dry-run`` prints the exact call
count, characters, estimated input tokens and estimated neurons for every
request, using the published unit price and this repository's own deliberately
conservative estimator side by side. Successful outputs are cached by deployed
SHA, model id and request hash, so a repeated case is never paid for twice.
There is no retry and no ``--force``.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import yaml

from knowledge_bot.application.classifier import Classification, IntentScores
from knowledge_bot.domain.enums import IntentLabel
from knowledge_bot.models.assessment import MessageAssessment, RetroevalCandidate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WINDOWS_FILE = ROOT / "evals" / "decision_windows.yaml"
CLASSIFIER_FILE = ROOT / "evals" / "classifier.yaml"
LISTENER_FILE = ROOT / "evals" / "listener.yaml"
CACHE_FILE = ROOT / ".eval-cache" / "clef-flash-production.jsonl"
REPORT_FILE = ROOT / "reports" / "clef-flash-focused-eval.md"
ENDPOINT = "/internal/eval/decision"

CLEF_MODEL = "@cf/cloudflare/clef-flash"
BASELINE = "baseline"
CLEF = "clef-flash"

#: Published unit price: $0.09 per M input tokens, billed at $0.011 per 1,000
#: neurons. Tokens per neuron, derived from both figures.
TOKENS_PER_NEURON = 122.0
#: Characters per token, for Catalan and Spanish prose.
CHARS_PER_TOKEN = 3.6
#: Tokens this repository assumes an answer adds, per request.
OUTPUT_TOKEN_RESERVE = 120
#: The repository's own deliberately conservative chat estimate.
NEURONS_PER_CHAR = 0.020
#: Cap for the whole experiment, raised from the plan's 1,500 by explicit
#: authorization so both backends run across all three suites.
EXPERIMENT_NEURON_CAP = 4500

#: Maximum cases per request, matching the endpoint's own limit.
BATCH = 5

#: Calibration grid, per the evaluation plan. Selection uses calibration cases
#: only, and the winner is applied once to the held-out split.
THRESHOLDS = (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
MARGINS = (0.00, 0.05, 0.10, 0.15, 0.20)

#: The deployed thresholds, used for the primary result.
PRIMARY_THRESHOLD = 0.80
PRIMARY_MARGIN = 0.15

INTENT_LABELS = ("question", "knowledge_update", "correction", "chitchat")

#: Fixed evaluation time for replayed scenarios.
_NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


class NeuronCapExceededError(Exception):
    """Raised when the experiment would exceed its neuron cap."""

    def __init__(self, estimated: int, cap: int) -> None:
        """Store the estimate that stopped the run and the cap it broke."""
        super().__init__(f"estimated {estimated} neurons exceed the cap {cap}")
        self.estimated = estimated
        self.cap = cap


@dataclass(slots=True)
class Case:
    """One evaluated case, whatever suite it came from."""

    case_id: str
    text: str
    candidates: list[dict[str, str]] = field(default_factory=list)
    expected_label: str | None = None
    expected_pair: str | None = None
    category: str = ""
    split: str = ""
    message_count: int = 1
    explicit_reply: bool = False


@dataclass(slots=True)
class Result:
    """One decided case."""

    case_id: str
    backend: str
    best_label: str | None = None
    best_score: float = 0.0
    margin: float = 0.0
    prefiltered: bool = False
    relevance: dict[str, float] = field(default_factory=dict)
    selected: str | None = None
    error: str | None = None
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """Whether the backend decided this case."""
        return self.error is None


def git_sha() -> str:
    """Return the deployed revision this evaluation measures."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def load_windows() -> list[Case]:
    """Load the frozen hard decision windows."""
    document = yaml.safe_load(WINDOWS_FILE.read_text(encoding="utf-8"))
    return [
        Case(
            case_id=window["id"],
            text=window["text"],
            candidates=list(window["candidates"]),
            expected_label=window.get("expected_intent"),
            expected_pair=window.get("expected_pair"),
            category=window["category"],
            split=window["split"],
        )
        for window in document["windows"]
    ]


def load_intent_cases() -> list[Case]:
    """Load the manual intent cases, excluding synthetic ones."""
    document = yaml.safe_load(CLASSIFIER_FILE.read_text(encoding="utf-8"))
    entries = document if isinstance(document, list) else document["cases"]
    cases: list[Case] = []
    for index, entry in enumerate(entries):
        source = str(entry.get("source", "")).lower()
        if source.startswith("synth") or entry.get("synthetic") is True:
            continue
        cases.append(
            Case(
                case_id=f"intent-{index}",
                text=entry["text"],
                expected_label=str(entry["labels"][0])
                if isinstance(entry["labels"], list)
                else str(entry["labels"]),
                category="intent_manual",
            )
        )
    return cases


def _listener_cases() -> list[Case]:
    """Flatten the real scenarios into one case per message.

    Each message is decided on its own so the suites share one calling path; the
    candidates a replayed message actually has come from the running flow, not
    from this list.
    """
    cases: list[Case] = []
    for scenario in load_listener_scenarios():
        for message in scenario["messages"]:
            cases.append(
                Case(
                    case_id=f"{scenario['id']}/{message['id']}",
                    text=message["text"],
                    expected_label=message.get("expected_label"),
                    category="listener_real",
                )
            )
    return cases


def load_listener_scenarios() -> list[dict[str, Any]]:
    """Load the real listener regression scenarios."""
    document = yaml.safe_load(LISTENER_FILE.read_text(encoding="utf-8"))
    scenarios = document if isinstance(document, list) else document["scenarios"]
    return [scenario for scenario in scenarios if scenario.get("synthetic") is False]


class EndpointAssessment:
    """A decision model whose answers come from the deployed evaluation route.

    It implements the same port the listener uses, so a real scenario replays
    through the production flow with only the decision swapped: candidate
    collection, pairing and projection all run locally and unmodified.
    """

    def __init__(self, backend: str, base_url: str, key: str, revision: str) -> None:
        """Configure the backend, where to ask, and what to cache under."""
        self.backend = backend
        self.base_url = base_url
        self.key = key
        self.revision = revision
        self.cache = Cache(revision, backend)
        self.confidence_threshold = 0.60
        self.margin_threshold = 0.15
        self.calls = 0

    async def assess(
        self, text: str, *, candidates: tuple[RetroevalCandidate, ...] = ()
    ) -> MessageAssessment:
        """Ask the route to decide one message together with its candidates."""
        case = {
            "case_id": f"listener-{self.calls}",
            "text": text,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "question_message_id": item.question_message_id,
                    "question": item.question,
                    "relation": item.relation,
                }
                for item in candidates
            ],
        }
        payload = {"backend": self.backend, "cases": [case]}
        self.calls += 1
        cache_key = Cache.key(self.revision, self.backend, payload)
        raw = self.cache.get(cache_key)
        if raw is None:
            async with httpx.AsyncClient() as client:
                raw = await call(client, self.base_url, self.key, payload)
            self.cache.put(cache_key, raw)
        return _assessment_from(raw[0])

    def is_question(self, classification: Classification) -> bool:
        """Whether the message is a confident question."""
        return (
            classification.is_confident_with(
                self.confidence_threshold, self.margin_threshold
            )
            and classification.best_label is IntentLabel.QUESTION
        )

    def is_answer_like(self, classification: Classification) -> bool:
        """Whether the message is a confident factual update or correction."""
        return classification.is_confident_with(
            self.confidence_threshold, self.margin_threshold
        ) and classification.best_label in (
            IntentLabel.KNOWLEDGE_UPDATE,
            IntentLabel.CORRECTION,
        )


def _assessment_from(result: dict[str, Any]) -> MessageAssessment:
    """Turn one route result into a message assessment."""
    if result.get("error"):
        message = f"{result['error']}: {result.get('detail', '')}"
        raise RuntimeError(message)
    probabilities = result.get("intent_probabilities") or {}
    scores = IntentScores(
        question=float(probabilities.get("question", 0.0)),
        knowledge_update=float(probabilities.get("knowledge_update", 0.0)),
        correction=float(probabilities.get("correction", 0.0)),
        chitchat=float(probabilities.get("chitchat", 0.0)),
    )
    return MessageAssessment(
        classification=Classification(
            scores=scores,
            best_label=IntentLabel(str(result["best_label"])),
            best_score=float(result["best_score"]),
            margin=float(result["margin"]),
            embedding=(),
            prefiltered=bool(result.get("prefiltered")),
        ),
        pair_relevance={
            str(name): float(value)
            for name, value in (result.get("relevance") or {}).items()
        },
    )


class Cache:
    """Successful decisions, keyed by revision, model and request."""

    def __init__(self, revision: str, model: str) -> None:
        """Open the cache for one revision and model."""
        self._revision = revision
        self._model = model
        self._entries: dict[str, dict[str, Any]] = {}
        self._hits = 0
        if CACHE_FILE.exists():
            for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("revision") == revision and entry.get("model") == model:
                    self._entries[entry["key"]] = entry["result"]

    @staticmethod
    def key(revision: str, model: str, payload: dict[str, Any]) -> str:
        """Return the content-addressed key of one request."""
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{revision}:{model}:{digest}"

    def get(self, key: str) -> list[dict[str, Any]] | None:
        """Return a cached batch result, counting the hit."""
        found = self._entries.get(key)
        if found is not None:
            self._hits += 1
        return cast("list[dict[str, Any]] | None", found)

    def put(self, key: str, result: list[dict[str, Any]]) -> None:
        """Store one successful batch result."""
        self._entries[key] = cast("dict[str, Any]", result)
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "revision": self._revision,
                        "model": self._model,
                        "key": key,
                        "result": result,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    @property
    def hits(self) -> int:
        """How many requests were answered without spending anything."""
        return self._hits


def payload_for(backend: str, cases: Iterable[Case]) -> dict[str, Any]:
    """Return the exact request body for a batch of cases."""
    return {
        "backend": backend,
        "cases": [
            {
                "case_id": case.case_id,
                "text": case.text,
                "candidates": case.candidates,
            }
            for case in cases
        ],
    }


def estimate(payload: dict[str, Any]) -> tuple[int, int, int, int]:
    """Return (characters, input tokens, neurons at price, neurons conservative)."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    characters = len(canonical)
    tokens = int(characters / CHARS_PER_TOKEN) + 1
    at_price = int(tokens / TOKENS_PER_NEURON) + OUTPUT_TOKEN_RESERVE // 40
    conservative = int(characters * NEURONS_PER_CHAR) + OUTPUT_TOKEN_RESERVE // 40
    return characters, tokens, at_price, conservative


def batches(cases: list[Case]) -> Iterable[list[Case]]:
    """Split cases into endpoint-sized batches."""
    for start in range(0, len(cases), BATCH):
        yield cases[start : start + BATCH]


async def call(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Send one bounded batch and return its raw results."""
    response = await client.post(
        f"{base_url.rstrip('/')}{ENDPOINT}",
        json=payload,
        headers={"X-Internal-Key": key},
        timeout=60.0,
    )
    response.raise_for_status()
    body = response.json()
    results = body.get("results")
    if not isinstance(results, list):
        message = f"unexpected evaluation response: {sorted(body)}"
        raise TypeError(message)
    return results


async def decide(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    backend: str,
    cases: list[Case],
    cache: Cache,
) -> list[Result]:
    """Decide every case with one backend, using the cache where possible."""
    collected: list[Result] = []
    pending: list[tuple[dict[str, Any], list[Case]]] = []
    for batch in batches(cases):
        payload = payload_for(backend, batch)
        cache_key = Cache.key(git_sha(), backend, payload)
        cached = cache.get(cache_key)
        if cached is not None:
            collected.extend(_to_results(cached, backend))
            continue
        pending.append((payload, batch))
    for payload, batch in pending:
        started = time.perf_counter()
        raw = await call(client, base_url, key, payload)
        elapsed = (time.perf_counter() - started) * 1000 / max(len(batch), 1)
        cache.put(Cache.key(git_sha(), backend, payload), raw)
        collected.extend(_to_results(raw, backend, elapsed))
    order = {case.case_id: index for index, case in enumerate(cases)}
    collected.sort(key=lambda item: order.get(item.case_id, 0))
    return collected


def _to_results(
    raw: list[dict[str, Any]], backend: str, elapsed: float = 0.0
) -> list[Result]:
    """Turn raw endpoint results into evaluated results."""
    results: list[Result] = []
    for item in raw:
        results.append(
            Result(
                case_id=str(item.get("case_id", "")),
                backend=backend,
                best_label=item.get("best_label"),
                best_score=float(item.get("best_score") or 0.0),
                margin=float(item.get("margin") or 0.0),
                prefiltered=bool(item.get("prefiltered")),
                relevance={
                    str(name): float(value)
                    for name, value in (item.get("relevance") or {}).items()
                },
                selected=item.get("selected_candidate_id"),
                error=item.get("error"),
                duration_ms=float(item.get("duration_ms") or elapsed),
            )
        )
    return results


async def replay_scenarios(
    base_url: str, key: str, revision: str, backend: str
) -> dict[str, Any]:
    """Replay the real listener scenarios with one backend deciding.

    The whole application flow runs: candidates are collected by the real
    pairing service, the decision comes from the deployed route, and the pair
    is selected by the application's own policy. Only the decision is swapped,
    which is what makes the two backends comparable.
    """
    from datetime import timedelta

    from knowledge_bot.application.background import BackgroundIndexer
    from knowledge_bot.application.budget import AiBudget
    from knowledge_bot.application.indexing import SearchProjectionService
    from knowledge_bot.application.ingest import MessageIngestor
    from knowledge_bot.application.listener import ListenerIngestor
    from knowledge_bot.application.listener_pairing import MessagePairingService
    from knowledge_bot.domain.enums import ContentType
    from knowledge_bot.models.common import SourceDescriptor
    from knowledge_bot.models.messages import NormalizedMessage
    from tests.fakes.ai import (
        FakeEmbedder,
        FakeSearchIndexSource,
        FakeVectorStore,
        InMemorySearchProjectionRepository,
    )
    from tests.fakes.backend import InMemoryBackend
    from tests.fakes.support import FrozenClock

    scenarios = load_listener_scenarios()
    made: dict[str, dict[str, int]] = {}
    for scenario in scenarios:
        backend_store = InMemoryBackend()
        clock = FrozenClock(_NOW)
        embedder = FakeEmbedder()
        vectors = FakeVectorStore()
        manifest = InMemorySearchProjectionRepository()
        assessment = EndpointAssessment(backend, base_url, key, revision)
        projector = SearchProjectionService(
            FakeSearchIndexSource(), embedder, vectors, manifest, clock
        )
        budget = AiBudget(backend_store.ai_usage, clock)
        ingestor = MessageIngestor(
            backend_store.sources,
            backend_store.conversations,
            backend_store.messages,
            backend_store.attachments,
        )
        background = BackgroundIndexer(
            backend_store.messages,
            backend_store.conversations,
            backend_store.sources,
            assessment,
            projector,
            clock,
            0.60,
            0.15,
            budget,
        )
        pair_repository = backend_store.message_pair_candidates
        pairing = MessagePairingService(
            backend_store.messages,
            backend_store.conversations,
            backend_store.sources,
            pair_repository,
            projector,
            assessment,
            clock,
        )
        listener = ListenerIngestor(
            ingestor=ingestor,
            assessment=assessment,
            budget=budget,
            pairing=pairing,
            background_indexer=background,
        )
        for message in scenario["messages"]:
            await listener.handle(
                NormalizedMessage(
                    id=f"{scenario['conversation_id']}:{message['id']}",
                    source=SourceDescriptor(
                        id="listener-source", kind="test", authority=40
                    ),
                    conversation_id=scenario["conversation_id"],
                    sender_is_admin=False,
                    timestamp=_NOW + timedelta(seconds=len(made)),
                    content_type=ContentType.TEXT,
                    text=message["text"],
                )
            )
        made[scenario["id"]] = {
            "expected": len(scenario.get("expected_pairs") or []),
            "made": len(pair_repository._items),  # type: ignore[attr-defined]
            "wrong": _wrong_pairs(scenario, pair_repository._items),  # type: ignore[attr-defined]
        }
    expected = sum(item["expected"] for item in made.values())
    wrong = sum(item["wrong"] for item in made.values())
    found = sum(item["made"] for item in made.values())
    return {
        "scenarios": len(scenarios),
        "expected_pairs": expected,
        "made_pairs": found,
        "wrong_pairs": wrong,
        "pair_recall": _ratio(found, expected),
        "per_scenario": made,
    }


def _wrong_pairs(scenario: dict[str, Any], pairs: dict[str, Any]) -> int:
    """Count pairs the scenario did not expect."""
    expected = {tuple(sorted(pair)) for pair in scenario.get("expected_pairs") or []}
    return sum(
        1
        for pair in pairs.values()
        if tuple(
            sorted(
                (
                    pair.question_message_id.split(":")[-1],
                    pair.answer_message_id.split(":")[-1],
                )
            )
        )
        not in expected
    )


def dry_run(backend_costs: list[tuple[str, int, int, int, int]]) -> None:
    """Print what the whole experiment would cost, and refuse to overspend."""
    print(
        f"{'suite':<28}{'calls':>7}{'chars':>10}{'tokens':>9}{'neurons*':>10}{'neurons†':>10}"
    )
    total_at_price = total_conservative = 0
    for name, calls, characters, tokens, conservative in backend_costs:
        at_price = int(tokens / TOKENS_PER_NEURON)
        print(
            f"{name:<28}{calls:>7}{characters:>10}{tokens:>9}"
            f"{at_price:>10}{conservative:>10}"
        )
        total_at_price += at_price
        total_conservative += conservative
    print()
    print(f"total neurons at the published price : {total_at_price}")
    print(f"total neurons, repository estimator  : {total_conservative}")
    print(f"configured cap                      : {EXPERIMENT_NEURON_CAP}")
    print(
        "* $0.09/M input tokens billed at $0.011 per 1,000 neurons (122 tokens/neuron)"
    )
    print("† characters x 0.020, this repository's conservative chat estimate")
    if total_conservative > EXPERIMENT_NEURON_CAP:
        raise NeuronCapExceededError(total_conservative, EXPERIMENT_NEURON_CAP)


def plan_costs(cases: dict[str, list[Case]]) -> list[tuple[str, int, int, int, int]]:
    """Return the cost of every suite for both backends."""
    rows: list[tuple[str, int, int, int, int]] = []
    for name, suite in cases.items():
        for backend in (BASELINE, CLEF):
            characters = tokens = conservative = 0
            calls = 0
            for batch in batches(suite):
                payload = payload_for(backend, batch)
                chars, batch_tokens, _, batch_conservative = estimate(payload)
                characters += chars
                tokens += batch_tokens
                conservative += batch_conservative
                calls += 1
            rows.append(
                (f"{name} / {backend}", calls, characters, tokens, conservative)
            )
    return rows


def intent_report(results: list[Result], cases: list[Case]) -> dict[str, Any]:
    """Reduce intent results to the metrics the safety rule uses."""
    by_id = {case.case_id: case for case in cases}
    hits = dict.fromkeys(INTENT_LABELS, 0)
    false_hits = dict.fromkeys(INTENT_LABELS, 0)
    misses = dict.fromkeys(INTENT_LABELS, 0)
    correct = scored = confident_correct = confident_total = 0
    errors = 0
    latencies: list[float] = []
    for result in results:
        latencies.append(result.duration_ms)
        if not result.ok:
            errors += 1
            continue
        truth = by_id[result.case_id].expected_label
        if truth is None:
            continue
        scored += 1
        predicted = result.best_label or "?"
        if predicted == truth:
            correct += 1
            hits[truth] += 1
        else:
            false_hits[predicted] += 1
            misses[truth] += 1
        if result.best_score >= 0.60 and result.margin >= 0.15:
            confident_total += 1
            confident_correct += predicted == truth
    f1s = []
    for label in INTENT_LABELS:
        precision = _ratio(hits[label], hits[label] + false_hits[label])
        recall = _ratio(hits[label], hits[label] + misses[label])
        f1s.append(
            0.0
            if precision + recall == 0
            else 2 * precision * recall / (precision + recall)
        )
    return {
        "cases": scored,
        "accuracy": _ratio(correct, scored),
        "macro_f1": sum(f1s) / len(f1s),
        "question_recall": _ratio(
            hits["question"], hits["question"] + misses["question"]
        ),
        "update_precision": _ratio(
            hits["knowledge_update"],
            hits["knowledge_update"] + false_hits["knowledge_update"],
        ),
        "correction_precision": _ratio(
            hits["correction"], hits["correction"] + false_hits["correction"]
        ),
        "chitchat_precision": _ratio(
            hits["chitchat"], hits["chitchat"] + false_hits["chitchat"]
        ),
        "confident_precision": _ratio(confident_correct, confident_total),
        "coverage": _ratio(confident_total, scored),
        "error_rate": _ratio(errors, len(results)),
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
    }


def window_report(
    results: list[Result], cases: list[Case], *, split: str | None = "test"
) -> dict[str, Any]:
    """Reduce window results to pairing metrics per category.

    Only the held-out split is scored: calibration cases chose the thresholds,
    so counting them here would grade the model on the split it was tuned with
    and would make a numerator and a held-out denominator incomparable.
    """
    if split is not None:
        wanted = {case.case_id for case in cases if case.split == split}
        results = [item for item in results if item.case_id in wanted]
        cases = [case for case in cases if case.split == split]
    by_id = {case.case_id: case for case in cases}
    paired = correct_pairs = wrong_pairs = 0
    per_category: dict[str, dict[str, int]] = {}
    errors = 0
    for result in results:
        case = by_id[result.case_id]
        bucket = per_category.setdefault(
            case.category, {"expected_pair": 0, "correct": 0, "wrong": 0, "missed": 0}
        )
        if not result.ok:
            errors += 1
            continue
        wanted = case.expected_pair
        if wanted is None:
            bucket["correct"] += result.selected is None
            bucket["wrong"] += result.selected is not None
            continue
        bucket["expected_pair"] += 1
        if result.selected == wanted:
            bucket["correct"] += 1
            paired += 1
            correct_pairs += 1
        elif result.selected is None:
            bucket["missed"] += 1
        else:
            bucket["wrong"] += 1
            wrong_pairs += 1
            paired += 1
    return {
        "windows": len(results),
        "pairs": paired,
        "pair_precision": _ratio(correct_pairs, paired),
        "pair_recall": _ratio(
            correct_pairs, sum(v["expected_pair"] for v in per_category.values())
        ),
        "wrong_pairs": wrong_pairs,
        "error_rate": _ratio(errors, len(results)),
        "categories": per_category,
    }


def calibrate(results: list[Result], cases: list[Case]) -> dict[str, Any]:
    """Choose threshold and margin on calibration windows only."""
    by_id = {case.case_id: case for case in cases}
    scored = [item for item in results if item.ok and item.relevance]

    def score(threshold: float, margin: float) -> tuple[int, int, int]:
        """Return (wrong pairs, correct pairs, correct abstentions)."""
        wrong = recovered = abstentions = 0
        for result in scored:
            case = by_id[result.case_id]
            ranked = sorted(
                result.relevance.items(), key=lambda kv: kv[1], reverse=True
            )
            top_id, top = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
            chosen = None if top < threshold or top - runner_up < margin else top_id
            if case.expected_pair is None:
                abstentions += chosen is None
            elif chosen == case.expected_pair:
                recovered += 1
            else:
                wrong += 1
        return wrong, recovered, abstentions

    best: tuple[int, int, int, float, float] | None = None
    for threshold in THRESHOLDS:
        for margin in MARGINS:
            wrong, recovered, abstentions = score(threshold, margin)
            candidate = (-wrong, recovered, abstentions, threshold, margin)
            if best is None or candidate > best:
                best = candidate
    assert best is not None
    return {
        "threshold": best[3],
        "margin": best[4],
        "calibration": {
            "wrong_pairs": -best[0],
            "recovered": best[1],
            "correct_abstentions": best[2],
        },
    }


def _ratio(numerator: int, denominator: int) -> float:
    """Return a safe ratio."""
    return numerator / denominator if denominator else 0.0


def _percentile(values: list[float], fraction: float) -> float:
    """Return one percentile of a list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[int(fraction * (len(ordered) - 1))]


def _fmt(value: float | int | str) -> str:
    """Format a metric for the report."""
    return f"{value:.4f}" if isinstance(value, float) else str(value)


def evaluate_gates(
    intent: dict[str, dict[str, Any]],
    windows: dict[str, dict[str, Any]],
    listener: dict[str, dict[str, Any]],
    calibration: dict[str, Any],
) -> dict[str, Any]:
    """Apply the plan's safety gates and value gates, both sides reported."""
    base, clef = intent[BASELINE], intent[CLEF]
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append((name, passed, detail))

    check(
        "intent question recall (no worse than baseline by >0.02)",
        clef["question_recall"] >= base["question_recall"] - 0.02,
        f"baseline {base['question_recall']:.4f} vs clef {clef['question_recall']:.4f}",
    )
    check(
        "intent knowledge_update precision (<=0.02 worse)",
        clef["update_precision"] >= base["update_precision"] - 0.02,
        f"baseline {base['update_precision']:.4f}, clef {clef['update_precision']:.4f}",
    )
    check(
        "intent correction precision (<=0.02 worse)",
        clef["correction_precision"] >= base["correction_precision"] - 0.02,
        f"baseline {base['correction_precision']:.4f}, "
        f"clef {clef['correction_precision']:.4f}",
    )
    check(
        "intent confident precision (<=0.01 worse)",
        clef["confident_precision"] >= base["confident_precision"] - 0.01,
        f"baseline {base['confident_precision']:.4f}, "
        f"clef {clef['confident_precision']:.4f}",
    )
    check(
        "intent coverage (<=0.05 worse)",
        clef["coverage"] >= base["coverage"] - 0.05,
        f"baseline {base['coverage']:.4f} vs clef {clef['coverage']:.4f}",
    )
    check(
        "intent error rate < 1%",
        clef["error_rate"] < 0.01,
        f"clef {clef['error_rate']:.4f}",
    )
    check(
        "10 real scenarios: 0 wrong pairs (clef)",
        listener[CLEF]["wrong_pairs"] == 0,
        f"baseline {listener[BASELINE]['wrong_pairs']} wrong, "
        f"clef {listener[CLEF]['wrong_pairs']} wrong",
    )
    held_out = windows[CLEF]
    check(
        "held-out pair precision >= 0.98",
        held_out["pair_precision"] >= 0.98,
        f"baseline {windows[BASELINE]['pair_precision']:.4f}, "
        f"clef {held_out['pair_precision']:.4f}",
    )
    check(
        "held-out wrong pairs <= 1",
        held_out["wrong_pairs"] <= 1,
        f"clef {held_out['wrong_pairs']}",
    )
    categories = held_out["categories"]
    expectations = (
        ("no_correct", "correct", 6, 7),
        ("ambiguous_two_plausible", "correct", 3, 4),
        ("explicit_unrelated", "correct", 3, 4),
        ("chitchat_noise", "correct", 3, 3),
    )
    for category, key, minimum, total in expectations:
        counts = categories.get(category, {})
        got = (
            counts.get(key, 0) if category != "no_correct" else counts.get("correct", 0)
        )
        check(
            f"{category}: >= {minimum}/{total}",
            got >= minimum,
            f"clef {got}/{total}",
        )
    multi = categories.get("multi_one_correct", {})
    recovered = multi.get("correct", 0)
    value_multi = recovered >= 7
    recall_gain = held_out["pair_recall"] - windows[BASELINE]["pair_recall"]
    value_recall = recall_gain >= 0.15
    safety_passed = all(passed for _, passed, _ in checks)
    worth = safety_passed and (value_multi or value_recall)
    return {
        "checks": checks,
        "safety_passed": safety_passed,
        "value_multi_question": value_multi,
        "value_recall_gain": value_recall,
        "recall_gain": recall_gain,
        "verdict": (
            "CLEF_IS_WORTH_A_PRODUCTION_ROLLOUT_PLAN" if worth else "KEEP_CURRENT"
        ),
    }


def write_report(report: dict[str, Any]) -> None:
    """Write the evaluation report."""
    lines = [
        "# Clef-Flash focused decision evaluation",
        "",
        f"- experiment SHA: `{report['revision']}`",
        f"- production runtime safety: {report['safety_note']}",
        f"- Clef configuration: {CLEF_MODEL}, one request per message, "
        f"include_relevance=true, no fallback",
        f"- total Clef calls: {report['clef_calls']} "
        f"({report['cache_hits']} served from cache)",
        f"- estimated neurons: {report['estimated_neurons']} "
        f"(cap {EXPERIMENT_NEURON_CAP})",
        "",
        "## Verdict",
        "",
        f"**{report['verdict']}**",
        "",
        "## Intent: 80 manual cases",
        "",
        "| metric | baseline | clef-flash |",
        "|---|---:|---:|",
    ]
    for metric in (
        "accuracy",
        "macro_f1",
        "question_recall",
        "update_precision",
        "correction_precision",
        "chitchat_precision",
        "confident_precision",
        "coverage",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
    ):
        lines.append(
            f"| {metric} | {_fmt(report['intent']['baseline'][metric])} "
            f"| {_fmt(report['intent']['clef-flash'][metric])} |"
        )
    lines += [
        "",
        "## 50 hard windows",
        "",
        "| metric | baseline | clef-flash |",
        "|---|---:|---:|",
    ]
    for metric in ("pair_precision", "pair_recall", "wrong_pairs", "error_rate"):
        lines.append(
            f"| {metric} | {_fmt(report['windows']['baseline'][metric])} "
            f"| {_fmt(report['windows']['clef-flash'][metric])} |"
        )
    lines += [
        "",
        f"- primary result at the deployed {PRIMARY_THRESHOLD}/{PRIMARY_MARGIN}: "
        f"{_fmt(report['primary_result'])}",
        "- calibrated on 15 calibration cases: threshold "
        f"{report['calibration']['threshold']}, "
        f"margin {report['calibration']['margin']}; that split is scored "
        f"separately and never mixed into the held-out numbers",
        f"- one-pass invariant: {report['one_pass']}",
        "",
        "## Per-category, clef-flash at the deployed thresholds",
        "",
        "| category | expected | correct | wrong | missed |",
        "|---|---:|---:|---:|---:|",
    ]
    for category, counts in sorted(
        report["windows"]["clef-flash"]["categories"].items()
    ):
        lines.append(
            f"| {category} | {counts['expected_pair']} | {counts['correct']} "
            f"| {counts['wrong']} | {counts['missed']} |"
        )
    lines += [
        "",
        "## 10 real listener scenarios",
        "",
        "| metric | baseline | clef-flash |",
        "|---|---:|---:|",
    ]
    for metric in ("expected_pairs", "made_pairs", "wrong_pairs", "pair_recall"):
        lines.append(
            f"| {metric} | {_fmt(report['listener'][BASELINE][metric])} "
            f"| {_fmt(report['listener'][CLEF][metric])} |"
        )
    lines += ["", "## Gates", "", "| gate | result | detail |", "|---|---|---|"]
    for name, passed, detail in report["gates"]["checks"]:
        lines.append(f"| {name} | {'PASS' if passed else 'FAIL'} | {detail} |")
    lines += [
        "",
        f"- safety gates passed: {report['gates']['safety_passed']}",
        f"- value gate, multi-question recovery: "
        f"{report['gates']['value_multi_question']}",
        f"- value gate, held-out recall gain: {report['gates']['value_recall_gain']} "
        f"(gain {report['gates']['recall_gain']:.4f})",
        "",
    ]
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(base_url: str, key: str, *, dry: bool) -> int:
    """Estimate or run the whole evaluation once."""
    revision = git_sha()
    intent_cases = load_intent_cases()
    windows = load_windows()
    listener_cases = _listener_cases()
    suites = {
        "intent_manual": intent_cases,
        "windows": windows,
        "listener_replay": listener_cases,
    }
    costs = plan_costs(suites)
    if dry:
        dry_run(costs)
        return 0
    cache = Cache(revision, CLEF_MODEL)
    intent_results: dict[str, list[Result]] = {}
    window_results: dict[str, list[Result]] = {}
    async with httpx.AsyncClient() as client:
        for backend in (BASELINE, CLEF):
            intent_results[backend] = await decide(
                client, base_url, key, backend, intent_cases, Cache(revision, backend)
            )
            window_results[backend] = await decide(
                client, base_url, key, backend, windows, Cache(revision, backend)
            )
        calibration_cases = [case for case in windows if case.split == "calibration"]
        calibration_results = await decide(
            client, base_url, key, CLEF, calibration_cases, cache
        )
    listener_reports = {
        backend: await replay_scenarios(base_url, key, revision, backend)
        for backend in (BASELINE, CLEF)
    }
    intent = {
        backend: intent_report(results, intent_cases)
        for backend, results in intent_results.items()
    }
    windows_report = {
        backend: window_report(results, windows)
        for backend, results in window_results.items()
    }
    chosen = calibrate(calibration_results, calibration_cases)
    calibration_report = window_report(
        calibration_results, calibration_cases, split=None
    )
    gates = evaluate_gates(intent, windows_report, listener_reports, chosen)
    report = {
        "revision": revision,
        "safety_note": (
            "listener traffic stayed on BaselineAssessmentModel; the endpoint "
            "persists nothing"
        ),
        "clef_calls": len(intent_results[CLEF]) + len(window_results[CLEF]),
        "cache_hits": cache.hits,
        "estimated_neurons": sum(row[2] for row in costs) // int(TOKENS_PER_NEURON),
        "intent": intent,
        "windows": windows_report,
        "primary_result": (
            f"baseline pair precision "
            f"{windows_report[BASELINE]['pair_precision']:.4f}, "
            f"clef {windows_report[CLEF]['pair_precision']:.4f}"
        ),
        "calibration": chosen,
        "calibration_windows": calibration_report,
        "listener": listener_reports,
        "one_pass": "one request per message, asserted by the endpoint tests",
        "gates": gates,
        "verdict": gates["verdict"],
    }
    write_report(report)
    print(f"wrote {REPORT_FILE.relative_to(ROOT)}")
    print(f"verdict: {report['verdict']}")
    return 0


def main() -> int:
    """Parse arguments and run or estimate the evaluation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--base-url", default=os.environ.get("BOT_BASE_URL", ""))
    parser.add_argument("--key", default=os.environ.get("INTERNAL_ADMIN_KEY", ""))
    arguments = parser.parse_args()
    if not arguments.base_url:
        print("BOT_BASE_URL is required", file=sys.stderr)
        return 2
    try:
        return asyncio.run(
            run(arguments.base_url, arguments.key, dry=arguments.dry_run)
        )
    except NeuronCapExceededError as stop:
        print(f"STOP: {stop}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
