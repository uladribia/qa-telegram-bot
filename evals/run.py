# SPDX-License-Identifier: MIT
"""Offline and live evals for the knowledge bot (spec §36, §45).

- **Offline** evals are deterministic and in-process (fakes, no network). They
  guard structure: trigger signals, intake decisions, seed versioning, citation
  format, and that every eval set is well formed.
- **Live** evals call the deployed Worker, so they measure real embedding
  retrieval, real answers, and real abstention. Run them manually.

Usage::

    uv run python -m evals.run offline
    uv run python -m evals.run live --base-url https://<worker>
"""

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from knowledge_bot.application.answer_question import render_source_line
from knowledge_bot.application.feedback import canonical_key_for
from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.application.retrieval import Evidence
from knowledge_bot.application.seed import SeedService
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.policies import is_ask_command
from tests.fakes.repositories import (
    InMemoryAttachmentRepository,
    InMemoryConversationRepository,
    InMemoryMessageRepository,
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
    InMemorySourceRepository,
)
from tests.fakes.support import FrozenClock

EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class EvalReport:
    """The outcome of one eval suite."""

    name: str
    total: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every case passed."""
        return self.total > 0 and self.passed == self.total

    @property
    def rate(self) -> float:
        """Pass rate in ``[0, 1]``."""
        return self.passed / self.total if self.total else 0.0

    def check(self, condition: bool, message: str) -> None:
        """Record one assertion."""
        self.total += 1
        if condition:
            self.passed += 1
        else:
            self.failures.append(message)

    def render(self) -> str:
        """Render a short Markdown report."""
        status = "PASS" if self.ok else "FAIL"
        lines = [
            f"### {self.name}: {status} — {self.passed}/{self.total} ({self.rate:.0%})"
        ]
        lines.extend(f"  - {failure}" for failure in self.failures[:8])
        return "\n".join(lines)


def load_cases(name: str) -> list[dict[str, object]]:
    """Load an eval YAML file.

    Args:
        name: File name inside ``evals/``.

    Returns:
        The parsed list of cases.
    """
    return yaml.safe_load((EVALS_DIR / name).read_text(encoding="utf-8"))


# --- Offline evals ---------------------------------------------------------


def eval_trigger() -> EvalReport:
    """The bot answers only when explicitly addressed."""
    report = EvalReport(name="trigger/addressing")
    addressed = ["/ask hola", "/ask@bot hola"]
    not_addressed = ["hola", "quan entrenen?", "gràcies!"]
    for text in addressed:
        report.check(is_ask_command(text), f"{text!r} should be addressed")
    for text in not_addressed:
        report.check(not is_ask_command(text), f"{text!r} should NOT be addressed")
    return report


def eval_abstention_set() -> EvalReport:
    """Every abstention case is well formed and non-empty."""
    report = EvalReport(name="abstention/set")
    cases = load_cases("abstention.yaml")
    report.check(len(cases) >= 15, f"expected >= 15 abstention cases, got {len(cases)}")
    for case in cases:
        question = case.get("question")
        report.check(
            isinstance(question, str) and bool(question),
            f"malformed abstention case: {case}",
        )
    return report


def eval_citation_format() -> EvalReport:
    """Web citations show URL + date; group citations show author + date."""
    report = EvalReport(name="citations/format")
    web = render_source_line(
        Evidence(
            source_id="q",
            label="Q&A",
            text="t",
            authority=90,
            similarity=1.0,
            question="Quan?",
            url="https://example.com/x",
            date="2026-01-01",
        )
    )
    group = render_source_line(
        Evidence(
            source_id="m",
            label="Grup",
            text="t",
            authority=40,
            similarity=1.0,
            author="\u00b7abc123",
            date="2026-01-02",
        )
    )
    report.check("https://example.com/x" in web, f"web citation lacks URL: {web}")
    report.check("2026-01-01" in web, f"web citation lacks date: {web}")
    report.check("abc123" in group, f"group citation lacks author: {group}")
    report.check("http" not in group, f"group citation should not show a URL: {group}")
    report.check(
        canonical_key_for("Com es demana?") == canonical_key_for("com es demana?"),
        "canonical key is not case-insensitive",
    )
    return report


def eval_seed_versioning() -> EvalReport:
    """Seeding creates items with versions and marks in-review entries."""
    report = EvalReport(name="seed/versioning")

    async def run() -> None:
        items = InMemoryQAItemRepository()
        versions = InMemoryQAVersionRepository()
        sources = InMemorySourceRepository()
        service = SeedService(
            qa_items=items,
            qa_versions=versions,
            sources=sources,
            ingestor=MessageIngestor(
                sources=InMemorySourceRepository(),
                conversations=InMemoryConversationRepository(),
                messages=InMemoryMessageRepository(),
                attachments=InMemoryAttachmentRepository(),
            ),
            clock=FrozenClock(NOW),
        )
        published = SeedQA(
            source_url="https://example.com/",
            source_anchor="qa-x",
            section="S",
            question="Com es demana l'equipament?",
            answer="Provar talles i enviar una llista.",
            status="published",
            retrieved_at=NOW,
        )
        review = published.model_copy(
            update={"source_anchor": "qa-y", "status": "in_review"}
        )
        created, skipped = await service.seed_qa([published, review])
        report.check(
            created == 2 and skipped == 0, f"expected 2 created, got {created}"
        )
        item = await items.get_by_canonical_key("qa-x")
        report.check(
            item is not None and item.current_version_id is not None,
            "published item has no current version",
        )
        review_item = await items.get_by_canonical_key("qa-y")
        report.check(
            review_item is not None and review_item.status.value == "under_review",
            "in-review entry is not under_review",
        )
        report.check(
            await sources.get("web_seed") is not None, "web_seed source not created"
        )

    asyncio.run(run())
    return report


def run_offline() -> list[EvalReport]:
    """Run every offline eval."""
    return [
        eval_trigger(),
        eval_abstention_set(),
        eval_citation_format(),
        eval_seed_versioning(),
    ]


# --- Live evals ------------------------------------------------------------


def _internal_headers() -> dict[str, str]:
    from knowledge_bot.infrastructure.settings import Settings

    return {"X-Internal-Key": Settings().internal_admin_key}


def eval_live_retrieval(base_url: str) -> EvalReport:
    """Measure retrieval recall against the deployed index.

    Uses the deployed reindex and the real embeddings via a query probe: the
    eval asks the Worker to answer each retrieval query and checks that the
    cited sources include the expected anchor's version id.
    """
    report = EvalReport(name="retrieval/recall@5")
    cases = load_cases("retrieval.yaml")
    try:
        httpx.post(
            f"{base_url}/internal/reindex", headers=_internal_headers(), timeout=600.0
        )
        # Vectorize indexing is eventually consistent: let the upserts settle.
        time.sleep(60)
        probe = httpx.post(
            f"{base_url}/internal/retrieve",
            headers=_internal_headers(),
            json={"queries": [case["query"] for case in cases]},
            timeout=600.0,
        )
        probe.raise_for_status()
    except httpx.HTTPError as error:
        report.check(False, f"live retrieval probe failed: {error}")
        return report
    payload = probe.json()
    results = payload.get("results", {}) if isinstance(payload, dict) else {}
    for case in cases:
        query = str(case["query"])
        accepted = {str(case["expected_anchor"])}
        extras = case.get("also_accepts")
        if isinstance(extras, list):
            accepted.update(str(item) for item in extras)
        anchors = results.get(query, [])
        report.check(
            any(anchor in anchors for anchor in accepted),
            f"miss: {query!r} -> {sorted(accepted)} not in {anchors}",
        )
    return report


def run_live(base_url: str) -> list[EvalReport]:
    """Run every live eval."""
    return [eval_live_retrieval(base_url)]


def main(argv: list[str] | None = None) -> int:
    """Run evals and print a report.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv``).

    Returns:
        Process exit code: 0 when every eval passes.
    """
    parser = argparse.ArgumentParser(description="Run knowledge-bot evals.")
    parser.add_argument("mode", choices=["offline", "live"])
    parser.add_argument("--base-url", default="")
    args = parser.parse_args(argv)

    reports = run_offline() if args.mode == "offline" else run_live(args.base_url)
    print(f"# {args.mode.capitalize()} evals\n")
    for report in reports:
        print(report.render())
        print()
    failed = [report for report in reports if not report.ok]
    print(f"Suites passed: {len(reports) - len(failed)}/{len(reports)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
