# SPDX-License-Identifier: MIT
"""Measure the two post-retrieval decisions: abstention and evidence selection.

The question this answers is narrow: would a decision model, given the same
shortlist cosine retrieval already chose, decide *better* than the current
route — a cosine floor, every item above it handed to the generator, and
abstention left to the generator's own judgement?

Every case states the truth it can carry. A case with no known answer item
scores abstention only. Thresholds are chosen on the calibration split and
applied once to the held-out split, so the held-out numbers are a measurement
rather than a search.

The comparison that matters is ``false_answer_rate``: answering a question the
corpus cannot answer. Abstaining too often is a product cost; answering wrongly
is a correctness cost, and the second one is what decides.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES_FILE = ROOT / "evals" / "answer_decisions.yaml"
CACHE_FILE = ROOT / ".eval-cache" / "clef-answer-decisions.jsonl"
REPORT_FILE = ROOT / "reports" / "clef-answer-decisions.md"
ENDPOINT = "/internal/eval/decision"
CLEF = "clef-flash"
MODEL = "@cf/cloudflare/clef-flash"

#: Published unit price: $0.09 per M input tokens billed at $0.011 per 1,000
#: neurons.
TOKENS_PER_NEURON = 122.0
CHARS_PER_TOKEN = 3.6
BATCH = 5
NEURON_CAP = 4500

#: The cosine floor the runtime applies today, kept as the reference route.
COSINE_FLOOR = 0.35

#: Grid searched on the calibration split only.
SUFFICIENCY_GRID = (0.50, 0.60, 0.70, 0.80, 0.90, 0.95)
SELECTION_GRID = (0.50, 0.60, 0.70, 0.80, 0.90)


@dataclass(slots=True)
class Case:
    """One post-retrieval decision case."""

    case_id: str
    family: str
    split: str
    question: str
    evidence: list[dict[str, str]]
    expected_answer_ids: list[str] | None
    expected_sufficient: bool
    reason: str


@dataclass(slots=True)
class Decided:
    """One decided case."""

    case_id: str
    sufficiency: float = 0.0
    relevance: dict[str, float] = field(default_factory=dict)
    selected: list[str] = field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """Whether the model decided this case."""
        return self.error is None


def load_cases() -> list[Case]:
    """Load the built decision cases."""
    document = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))
    return [
        Case(
            case_id=case["case_id"],
            family=case["family"],
            split=case["split"],
            question=case["question"],
            evidence=case["evidence"],
            expected_answer_ids=case["expected_answer_ids"],
            expected_sufficient=bool(case["expected_sufficient"]),
            reason=case.get("reason", ""),
        )
        for case in document["cases"]
    ]


def _key(revision: str, payload: dict[str, Any]) -> str:
    """Return the content-addressed cache key of one request."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"{revision}:{hashlib.sha256(canonical.encode()).hexdigest()}"


class Cache:
    """Successful decisions keyed by revision and request."""

    def __init__(self, revision: str) -> None:
        """Load the cache for one revision."""
        self.revision = revision
        self.hits = 0
        self.entries: dict[str, dict[str, Any]] = {}
        if CACHE_FILE.exists():
            for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("revision") == revision:
                    self.entries[entry["key"]] = entry["result"]

    def get(self, key: str) -> dict[str, Any] | None:
        """Return a cached response, counting the hit."""
        found = self.entries.get(key)
        if found is not None:
            self.hits += 1
        return found

    def put(self, key: str, result: dict[str, Any]) -> None:
        """Store one successful response."""
        self.entries[key] = result
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"revision": self.revision, "key": key, "result": result},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def _payload(cases: list[Case]) -> dict[str, Any]:
    """Return the exact request body for a batch."""
    return {
        "backend": CLEF,
        "cases": [
            {
                "case_id": case.case_id,
                "text": case.question,
                "question": case.question,
                "evidence": case.evidence,
            }
            for case in cases
        ],
    }


def _batches(cases: list[Case]) -> Iterable[list[Case]]:
    """Split cases into endpoint-sized batches."""
    for start in range(0, len(cases), BATCH):
        yield cases[start : start + BATCH]


def estimate(cases: list[Case]) -> tuple[int, int, int]:
    """Return (calls, characters, neurons) for the whole suite."""
    calls = characters = tokens = 0
    for batch in _batches(cases):
        body = json.dumps(_payload(batch), ensure_ascii=False, sort_keys=True)
        calls += 1
        characters += len(body)
        tokens += int(len(body) / CHARS_PER_TOKEN) + 1
    return calls, characters, int(tokens / TOKENS_PER_NEURON)


async def decide(
    client: httpx.AsyncClient, base_url: str, key: str, cases: list[Case], cache: Cache
) -> list[Decided]:
    """Decide every case once, in bounded batches, never retrying."""
    decided: list[Decided] = []
    pending: list[list[Case]] = []
    for batch in _batches(cases):
        cached = cache.get(_key(cache.revision, _payload(batch)))
        if cached is None:
            pending.append(batch)
            continue
        decided.extend(_to_decided(cached["results"]))
    for batch in pending:
        response = await client.post(
            f"{base_url.rstrip('/')}{ENDPOINT}",
            json=_payload(batch),
            headers={"X-Internal-Key": key},
            timeout=120.0,
        )
        response.raise_for_status()
        body = response.json()
        cache.put(_key(cache.revision, _payload(batch)), body)
        decided.extend(_to_decided(body["results"]))
    order = {case.case_id: index for index, case in enumerate(cases)}
    decided.sort(key=lambda item: order.get(item.case_id, 0))
    return decided


def _to_decided(results: list[dict[str, Any]]) -> list[Decided]:
    """Turn raw endpoint results into decided cases."""
    decided: list[Decided] = []
    for item in results:
        decided.append(
            Decided(
                case_id=str(item.get("case_id", "")),
                sufficiency=float(item.get("sufficiency") or 0.0),
                relevance={
                    str(name): float(value)
                    for name, value in (item.get("evidence_relevance") or {}).items()
                },
                selected=[str(name) for name in (item.get("selected") or [])],
                error=item.get("error"),
                duration_ms=float(item.get("duration_ms") or 0.0),
            )
        )
    return decided


def current_route(case: Case) -> dict[str, Any]:
    """Describe what the deployed route does with this case, without any model.

    Today: cosine decides what is in the shortlist, the floor decides what
    survives, everything surviving is handed to the generator, and the generator
    alone may abstain. There is no decision model between them, so the honest
    reference is "answer whenever anything survived retrieval" with the
    generator's measured abstention reported separately from the gold eval.
    """
    survived = [item["evidence_id"] for item in case.evidence]
    return {
        "answered": bool(survived),
        "selected": survived,
        "evidence_count": len(case.evidence),
    }


def score(
    decided: list[Decided],
    cases: list[Case],
    *,
    sufficiency: float,
    selection: float,
) -> dict[str, Any]:
    """Score both decisions at one threshold pair."""
    by_id = {case.case_id: case for case in cases}
    answered_unanswerable = 0
    unanswerable = 0
    abstained_answerable = 0
    answerable = 0
    selected_right = scored = 0
    false_selection = 0
    errors = 0
    latencies: list[float] = []
    items_passed = 0
    items_available = 0
    for item in decided:
        latencies.append(item.duration_ms)
        case = by_id[item.case_id]
        items_available += len(case.evidence)
        if not item.ok:
            errors += 1
            answered_unanswerable += 1
            unanswerable += 1
            continue
        would_answer = item.sufficiency >= sufficiency
        passed = [
            name
            for name in sorted(
                item.relevance,
                key=lambda name: item.relevance[name],
                reverse=True,
            )
            if would_answer and item.relevance[name] >= selection
        ]
        items_passed += len(passed)
        if case.expected_sufficient:
            answerable += 1
            abstained_answerable += not would_answer
        else:
            unanswerable += 1
            answered_unanswerable += would_answer
        if case.expected_answer_ids is None:
            continue
        hits = [name for name in passed if name in case.expected_answer_ids]
        false_selection += len(
            [name for name in passed if name not in case.expected_answer_ids]
        )
        if hits:
            selected_right += 1
            scored += 1
    return {
        "sufficiency": sufficiency,
        "selection": selection,
        "answerable_cases": answerable,
        "unanswerable_cases": unanswerable,
        "false_answer_rate": _ratio(answered_unanswerable, unanswerable),
        "abstention_recall": _ratio(unanswerable - answered_unanswerable, unanswerable),
        "coverage": _ratio(answerable - abstained_answerable, answerable),
        "selection_cases": scored,
        "right_item_selected": _ratio(selected_right, scored),
        "false_selection_per_case": _ratio(false_selection, max(scored, 1)),
        "items_passed": _ratio(items_passed, max(items_available, 1)),
        "errors": errors,
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
    }


def current_route_score(cases: list[Case]) -> dict[str, Any]:
    """Score the deployed route on the same cases, for the head-to-head."""
    answered_unanswerable = answerable = abstained = 0
    selected_right = scored = 0
    items_passed = items_available = 0
    for case in cases:
        outcome = current_route(case)
        items_available += len(case.evidence)
        items_passed += len(outcome["selected"])
        if case.expected_sufficient:
            answerable += 1
            abstained += not outcome["answered"]
        else:
            answered_unanswerable += outcome["answered"]
        if case.expected_answer_ids is None:
            continue
        if [name for name in outcome["selected"] if name in case.expected_answer_ids]:
            selected_right += 1
            scored += 1
    return {
        "sufficiency": None,
        "selection": None,
        "answerable_cases": answerable,
        "unanswerable_cases": len(cases) - answerable,
        "false_answer_rate": _ratio(answered_unanswerable, len(cases) - answerable),
        "abstention_recall": _ratio(
            len(cases) - answerable - answered_unanswerable, len(cases) - answerable
        ),
        "coverage": _ratio(answerable - abstained, answerable),
        "selection_cases": scored,
        "right_item_selected": _ratio(selected_right, scored),
        "false_selection_per_case": 0.0,
        "items_passed": _ratio(items_passed, max(items_available, 1)),
        "errors": 0,
        "latency_p50_ms": 0.0,
        "latency_p95_ms": 0.0,
    }


def calibrate(decided: list[Decided], cases: list[Case]) -> dict[str, Any]:
    """Choose both thresholds on the calibration split only.

    The objective is stated up front rather than discovered: never answer an
    unanswerable question, then recover as many answerable ones as possible,
    then pass as few items as possible to the generator.
    """
    best: tuple[float, ...] | None = None
    chosen: dict[str, float] = {"sufficiency": 0.0, "selection": 0.0}
    for sufficiency in SUFFICIENCY_GRID:
        for selection in SELECTION_GRID:
            metrics = score(
                decided, cases, sufficiency=sufficiency, selection=selection
            )
            candidate = (
                metrics["false_answer_rate"],
                -metrics["coverage"],
                metrics["items_passed"],
                sufficiency,
                selection,
            )
            if best is None or candidate < best:
                best = candidate
                chosen = {"sufficiency": sufficiency, "selection": selection}
    return chosen


def _ratio(numerator: float, denominator: float) -> float:
    """Return a safe ratio."""
    return numerator / denominator if denominator else 0.0


def _percentile(values: list[float], fraction: float) -> float:
    """Return one percentile."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[int(fraction * (len(ordered) - 1))]


def git_sha() -> str:
    """Return the revision under measurement."""
    import subprocess

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


def _fmt(value: float | int | str | None) -> str:
    """Format a metric."""
    return f"{value:.4f}" if isinstance(value, float) else str(value)


async def run(base_url: str, key: str, *, dry: bool) -> int:
    """Estimate or run the whole post-retrieval evaluation."""
    cases = load_cases()
    calls, characters, neurons = estimate(cases)
    print(
        f"cases {len(cases)} · calls {calls} · characters {characters}"
        f" · neurons ~{neurons}"
    )
    if neurons > NEURON_CAP:
        print(f"STOP: estimated {neurons} neurons exceed the cap {NEURON_CAP}")
        return 1
    if dry:
        print("dry run: 0 remote calls")
        return 0
    revision = git_sha()
    cache = Cache(revision)
    async with httpx.AsyncClient() as client:
        decided = await decide(client, base_url, key, cases, cache)
    calibration_cases = [case for case in cases if case.split == "calibration"]
    test_cases = [case for case in cases if case.split == "test"]
    calibration_decided = [
        item
        for item in decided
        if item.case_id in {case.case_id for case in calibration_cases}
    ]
    chosen = calibrate(calibration_decided, calibration_cases)
    held = score(decided, test_cases, **chosen)
    report = {
        "revision": revision,
        "model": MODEL,
        "calls": calls,
        "neurons": neurons,
        "cache_hits": cache.hits,
        "chosen": chosen,
        "held_out": held,
        "current": current_route_score(test_cases),
        "families": {
            family: score(
                decided,
                [case for case in test_cases if case.family == family],
                **chosen,
            )
            for family in ("gold", "synthetic")
        },
        "calibration_score": score(calibration_decided, calibration_cases, **chosen),
    }
    _write(report)
    print(f"wrote {REPORT_FILE.relative_to(ROOT)}")
    print(
        f"clef false_answer_rate={held['false_answer_rate']:.4f} "
        f"coverage={held['coverage']:.4f} "
        f"right_item={held['right_item_selected']:.4f} | "
        f"current false_answer_rate={report['current']['false_answer_rate']:.4f}"
    )
    return 0


def _write(report: dict[str, Any]) -> None:
    """Write the report."""
    lines = [
        "# Clef-Flash post-retrieval decisions",
        "",
        f"- revision: `{report['revision']}`",
        f"- model: {report['model']}, one request per case, no fallback",
        "- cases: held-out scored, calibration chosen",
        f"- chosen on calibration: sufficiency "
        f"{report['chosen']['sufficiency']}, selection {report['chosen']['selection']}",
        f"- calls: {report['calls']} ({report['cache_hits']} cached), "
        f"estimated neurons: {report['neurons']}",
        "",
        "## Head to head, held-out cases",
        "",
        "| metric | current route | clef |",
        "|---|---:|---:|",
    ]
    for metric in (
        "false_answer_rate",
        "abstention_recall",
        "coverage",
        "right_item_selected",
        "false_selection_per_case",
        "items_passed",
        "latency_p50_ms",
        "latency_p95_ms",
    ):
        lines.append(
            f"| {metric} | {_fmt(report['current'][metric])} "
            f"| {_fmt(report['held_out'][metric])} |"
        )
    lines += [
        "",
        "## By family, clef",
        "",
        "| family | false answers | coverage | right item |",
        "|---|---:|---:|---:|",
    ]
    for family, metrics in report["families"].items():
        lines.append(
            f"| {family} | {_fmt(metrics['false_answer_rate'])} "
            f"| {_fmt(metrics['coverage'])} | {_fmt(metrics['right_item_selected'])} |"
        )
    lines += [
        "",
        "## Reading the comparison",
        "",
        "`false_answer_rate` is the number that decides: answering a question the",
        "corpus cannot answer. `coverage` is the product cost of being strict, and",
        "`items_passed` is the token cost of handing the generator too much.",
        "",
    ]
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    """Parse arguments and estimate or run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--base-url", default=os.environ.get("BOT_BASE_URL", ""))
    parser.add_argument("--key", default=os.environ.get("INTERNAL_ADMIN_KEY", ""))
    arguments = parser.parse_args()
    if not arguments.base_url:
        print("BOT_BASE_URL is required", file=sys.stderr)
        return 2
    return asyncio.run(run(arguments.base_url, arguments.key, dry=arguments.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
