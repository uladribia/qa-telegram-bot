# SPDX-License-Identifier: MIT
"""Check the local System-One decision service, and optionally score it.

Two jobs, because they answer different questions at different moments:

``--smoke`` (default)
    Can the configured decision service answer one canonical System-One request?
    This is the runtime gate: it proves the route, the wire contract and the
    strict parser agree with a real server before the listener depends on it.
    It says nothing about quality.

``--eval``
    Score that service on the repository's 500-case Catalan/Spanish intent test
    split and write ``reports/decision-service.md``. Useful to know what the
    local model actually does; **not a release gate**. The local runtime exists
    for end-to-end fidelity, and the numbers here describe a testing model on
    one machine, not the bot that serves the group.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from knowledge_bot.application.assessment import (  # noqa: E402
    build_message_decision_request,
    parse_intent_decision,
)
from knowledge_bot.domain.enums import IntentLabel  # noqa: E402
from knowledge_bot.infrastructure.local.system_one import (  # noqa: E402
    HttpSystemOneTransport,
)

DEFAULT_BASE_URL = os.environ.get("DECISION_BASE_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("DECISION_MODEL", "tev1:0.8b")
TEST_SPLIT = ROOT / "data" / "classifier" / "test.jsonl"
REPORT = ROOT / "reports" / "decision-service.md"
TIMEOUT_SECONDS = 60.0
#: The same policy the application applies to its own classifications.
CONFIDENCE_THRESHOLD = 0.60
MARGIN_THRESHOLD = 0.15


@dataclass(frozen=True, slots=True)
class IntentReport:
    """How a decision service classified the human intent test split."""

    examples: int
    accuracy: float
    macro_f1: float
    question_recall: float
    update_precision: float
    correction_precision: float
    confident_precision: float
    coverage: float
    p95_latency_ms: float


def _transport(base_url: str) -> HttpSystemOneTransport:
    """Build a transport for one decision service."""
    return HttpSystemOneTransport(
        client=httpx.AsyncClient(timeout=TIMEOUT_SECONDS),
        base_url=base_url,
        timeout_seconds=TIMEOUT_SECONDS,
    )


def _test_examples() -> list[dict[str, Any]]:
    """Return the human-labelled intent test split."""
    if not TEST_SPLIT.exists():
        message = f"missing {TEST_SPLIT}; run `make test` data first"
        raise SystemExit(message)
    return [
        json.loads(line)
        for line in TEST_SPLIT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def smoke(base_url: str, model: str) -> int:
    """Send one canonical intent request and report whether it answers."""
    transport = _transport(base_url)
    state, questions = build_message_decision_request(
        "Quan entrenen els entrenaments?", ()
    )
    started = time.monotonic()
    try:
        payload = await transport.decide(model=model, state=state, questions=questions)
        scores, label, margin = parse_intent_decision(payload)
    except Exception as error:
        print(f"DECISION_SERVICE_OK=false ({type(error).__name__}: {error})")
        await transport.client.aclose()
        return 1
    elapsed = (time.monotonic() - started) * 1000
    await transport.client.aclose()
    print(f"DECISION_SERVICE_OK=true ({elapsed:.0f} ms)")
    print(f"model={model} label={label.value} margin={margin:.3f}")
    print(
        "probabilities="
        + json.dumps(
            {
                "question": scores.question,
                "knowledge_update": scores.knowledge_update,
                "correction": scores.correction,
                "chitchat": scores.chitchat,
            },
            sort_keys=True,
        )
    )
    return 0


async def _classify(
    transport: HttpSystemOneTransport, model: str, text: str
) -> tuple[str, float, float, float]:
    """Classify one text and return (label, top score, margin, latency ms)."""
    state, questions = build_message_decision_request(text, ())
    started = time.monotonic()
    payload = await transport.decide(model=model, state=state, questions=questions)
    elapsed = (time.monotonic() - started) * 1000
    scores, label, margin = parse_intent_decision(payload)
    return label.value, scores.best()[1], margin, elapsed


async def evaluate(base_url: str, model: str) -> int:
    """Score the decision service on the intent test split and write a report."""
    transport = _transport(base_url)
    truth: list[str] = []
    predicted: list[str] = []
    scores: list[float] = []
    margins: list[float] = []
    latencies: list[float] = []
    for example in _test_examples():
        label, score, margin, elapsed = await _classify(
            transport, model, example["text"]
        )
        truth.append(example["label"])
        predicted.append(label)
        scores.append(score)
        margins.append(margin)
        latencies.append(elapsed)
    await transport.client.aclose()

    report = _summarise(truth, predicted, scores, margins, latencies)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        "\n".join(
            (
                "# Decision service report",
                "",
                f"- service: `{base_url}`",
                f"- model: `{model}`",
                f"- examples: {report.examples} (data/classifier/test.jsonl)",
                "- local testing runtime only; not a release gate",
                "",
                "| metric | result |",
                "|---|---|",
                f"| accuracy | {report.accuracy:.4f} |",
                f"| macro F1 | {report.macro_f1:.4f} |",
                f"| question recall | {report.question_recall:.4f} |",
                f"| knowledge_update precision | {report.update_precision:.4f} |",
                f"| correction precision | {report.correction_precision:.4f} |",
                f"| precision among confident | {report.confident_precision:.4f} |",
                f"| coverage (non-ambiguous) | {report.coverage:.4f} |",
                f"| latency p95 | {report.p95_latency_ms:.0f} ms |",
                "",
            )
        ),
        encoding="utf-8",
    )
    print(REPORT.relative_to(ROOT))
    print(
        f"accuracy={report.accuracy:.4f} macro_f1={report.macro_f1:.4f} "
        f"confident_precision={report.confident_precision:.4f} "
        f"p95={report.p95_latency_ms:.0f}ms"
    )
    return 0


def _summarise(
    truth: list[str],
    predicted: list[str],
    scores: list[float],
    margins: list[float],
    latencies: list[float],
) -> IntentReport:
    """Reduce predictions to the numbers a reader can act on."""
    labels = [label.value for label in IntentLabel]
    hits = dict.fromkeys(labels, 0)
    false_hits = dict.fromkeys(labels, 0)
    misses = dict.fromkeys(labels, 0)
    for expected, actual in zip(truth, predicted, strict=True):
        if expected == actual:
            hits[expected] += 1
        else:
            false_hits[actual] += 1
            misses[expected] += 1
    f1s = []
    for label in labels:
        precision = _ratio(hits[label], hits[label] + false_hits[label])
        recall = _ratio(hits[label], hits[label] + misses[label])
        f1s.append(
            0.0
            if precision + recall == 0
            else 2 * precision * recall / (precision + recall)
        )
    confident = [
        index
        for index in range(len(truth))
        if scores[index] >= CONFIDENCE_THRESHOLD and margins[index] >= MARGIN_THRESHOLD
    ]
    ordered = sorted(latencies)
    return IntentReport(
        examples=len(truth),
        accuracy=_ratio(
            sum(1 for a, b in zip(truth, predicted, strict=True) if a == b), len(truth)
        ),
        macro_f1=sum(f1s) / len(f1s),
        question_recall=_ratio(hits["question"], hits["question"] + misses["question"]),
        update_precision=_ratio(
            hits["knowledge_update"],
            hits["knowledge_update"] + false_hits["knowledge_update"],
        ),
        correction_precision=_ratio(
            hits["correction"], hits["correction"] + false_hits["correction"]
        ),
        confident_precision=_ratio(
            sum(1 for index in confident if truth[index] == predicted[index]),
            len(confident),
        ),
        coverage=_ratio(len(confident), len(truth)),
        p95_latency_ms=ordered[int(0.95 * (len(ordered) - 1))] if ordered else 0.0,
    )


def _ratio(numerator: int, denominator: int) -> float:
    """Return a safe ratio."""
    return numerator / denominator if denominator else 0.0


def main() -> int:
    """Run the smoke or the evaluation, as asked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval", action="store_true", help="Score and write a report.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    arguments = parser.parse_args()
    if arguments.eval:
        return asyncio.run(evaluate(arguments.base_url, arguments.model))
    return asyncio.run(smoke(arguments.base_url, arguments.model))


if __name__ == "__main__":
    raise SystemExit(main())
