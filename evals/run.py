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
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from knowledge_bot.application.answer_question import AnswerService, render_source_line
from knowledge_bot.application.feedback import FeedbackService, canonical_key_for
from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.application.retrieval import (
    Evidence,
    RetrievalService,
    RetrievedEvidence,
)
from knowledge_bot.application.seed import SeedService
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.entities import BotAnswer, QAItem, QAVersion
from knowledge_bot.domain.enums import AnswerMode, QAStatus, ReviewAction
from knowledge_bot.domain.identity import source_instance_id
from knowledge_bot.domain.policies import is_ask_command
from knowledge_bot.ports.generator import GenerationOutput
from tests.fakes.ai import (
    FakeEmbedder,
    FakeGenerator,
    FakeLexicalIndex,
    FakeVectorStore,
)
from tests.fakes.repositories import (
    InMemoryAttachmentRepository,
    InMemoryBotAnswerRepository,
    InMemoryConversationRepository,
    InMemoryFeedbackRepository,
    InMemoryMessageRepository,
    InMemoryQAEvidenceRepository,
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
    InMemorySourceRepository,
)
from tests.fakes.support import FrozenClock
from tests.fakes.transactions import InMemoryCorrectionCommitStore

EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class EvalReport:
    """The outcome of one eval suite."""

    name: str
    total: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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

    def warn(self, message: str) -> None:
        """Record an advisory finding that never fails the suite."""
        self.warnings.append(message)

    def render(self) -> str:
        """Render a short Markdown report."""
        status = "PASS" if self.ok else "FAIL"
        lines = [
            f"### {self.name}: {status} — {self.passed}/{self.total} ({self.rate:.0%})"
        ]
        lines.extend(f"  - {failure}" for failure in self.failures[:8])
        lines.extend(f"  ~ {warning}" for warning in self.warnings[:5])
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
    cases = [
        *load_cases("abstention.yaml"),
        *load_cases("abstention_synthetic.yaml"),
    ]
    report.check(
        len(cases) >= 250,
        f"expected >= 250 abstention cases, got {len(cases)}",
    )
    for case in cases:
        question = case.get("question")
        report.check(
            isinstance(question, str),
            f"malformed abstention case: {case}",
        )
        if case.get("synthetic") is True:
            report.check(
                case.get("expected") == "abstain",
                f"synthetic case must be labelled abstain: {case}",
            )
            report.check(
                isinstance(case.get("reason"), str) and bool(case.get("reason")),
                f"synthetic abstention case lacks a reason: {case}",
            )
    return report


def eval_classifier_dataset() -> EvalReport:
    """Validate the expanded intent-classifier dataset shape."""
    report = EvalReport(name="classifier/dataset")
    cases = load_cases("classifier.yaml")
    report.check(
        len(cases) >= 200,
        f"expected >= 200 classifier cases, got {len(cases)}",
    )
    allowed = {"question", "knowledge_update", "correction", "chitchat"}
    for case in cases:
        text = case.get("text")
        labels = case.get("labels")
        report.check(
            isinstance(text, str) and bool(text),
            f"malformed classifier text: {case}",
        )
        report.check(
            isinstance(labels, list) and bool(labels) and set(labels) <= allowed,
            f"malformed classifier labels: {case}",
        )
    return report


def eval_classifier_splits() -> EvalReport:
    """Validate the 500 + 500 JSONL classifier splits are disjoint and balanced."""
    report = EvalReport(name="classifier/splits")
    allowed = {"question", "knowledge_update", "correction", "chitchat"}
    split_cases: dict[str, list[dict]] = {}
    for name in ("train.jsonl", "test.jsonl"):
        path = EVALS_DIR.parent / "data" / "classifier" / name
        if not path.exists():
            report.check(False, f"missing classifier split: {path}")
            split_cases[name] = []
            continue
        cases = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        split_cases[name] = cases
        report.check(
            len(cases) == 500, f"{name} must hold exactly 500 cases, got {len(cases)}"
        )
        for case in cases:
            report.check(
                case.get("label") in allowed and bool(case.get("text")),
                f"malformed case in {name}: {case}",
            )
            report.check(
                case.get("source") in {"human", "synthetic"},
                f"unknown provenance in {name}: {case}",
            )
    train_keys = {
        " ".join(case["text"].casefold().split()) for case in split_cases["train.jsonl"]
    }
    test_keys = {
        " ".join(case["text"].casefold().split()) for case in split_cases["test.jsonl"]
    }
    report.check(
        not train_keys & test_keys,
        f"train/test overlap after normalization: {sorted(train_keys & test_keys)[:3]}",
    )
    human_train = sum(
        1 for case in split_cases["train.jsonl"] if case.get("source") == "human"
    )
    report.check(
        human_train > 0, "the committed human gold cases must stay in the train split"
    )
    return report


def eval_retrieval_dataset() -> EvalReport:
    """Validate the 500-query retrieval eval over existing seed anchors."""
    report = EvalReport(name="retrieval/dataset")
    gold = load_cases("retrieval.yaml")
    synthetic = load_cases("retrieval_synthetic.yaml")
    total = len(gold) + len(synthetic)
    report.check(total == 500, f"retrieval eval must total 500 queries, got {total}")
    seed_items = json.loads(
        (EVALS_DIR.parent / "data" / "seed" / "qa.json").read_text(encoding="utf-8")
    )
    anchors = {item.get("source_anchor") for item in seed_items}
    queries: set[str] = set()
    for name, cases in (
        ("retrieval.yaml", gold),
        ("retrieval_synthetic.yaml", synthetic),
    ):
        for case in cases:
            query = case.get("query")
            anchor = case.get("expected_anchor")
            report.check(
                isinstance(query, str) and bool(query),
                f"malformed retrieval query in {name}: {case}",
            )
            report.check(
                anchor in anchors,
                f"unknown anchor {anchor!r} in {name} (must exist in the seed)",
            )
            normalized = " ".join(str(query).casefold().split())
            report.check(
                normalized not in queries,
                f"duplicate retrieval query in {name}: {query!r}",
            )
            queries.add(normalized)
            if case.get("source") == "synthetic":
                report.check(
                    normalized not in _classifier_split_texts(),
                    f"synthetic retrieval query appears in classifier data: {query!r}",
                )
    return report


def _classifier_split_texts() -> set[str]:
    """Return normalized texts of both classifier splits."""
    texts: set[str] = set()
    for name in ("train.jsonl", "test.jsonl"):
        path = EVALS_DIR.parent / "data" / "classifier" / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                texts.add(" ".join(json.loads(line)["text"].casefold().split()))
    return texts


def eval_listener_dataset() -> EvalReport:
    """Validate question labels and expected temporal pairs."""
    report = EvalReport(name="listener/dataset")
    cases = load_cases("listener.yaml")
    report.check(len(cases) >= 50, f"expected >= 50 listener cases, got {len(cases)}")
    allowed = {"question", "answer_like", "chitchat", "knowledge_update", "correction"}
    for case in cases:
        messages = case.get("messages")
        report.check(
            isinstance(messages, list) and len(messages) >= 2,
            f"listener case needs messages: {case}",
        )
        if not isinstance(messages, list):
            continue
        ids = {
            str(message.get("id")) for message in messages if isinstance(message, dict)
        }
        report.check(len(ids) == len(messages), f"listener ids must be unique: {case}")
        for message in messages:
            report.check(
                isinstance(message, dict) and message.get("expected_label") in allowed,
                f"listener message label is invalid: {message}",
            )
        pairs = case.get("expected_pairs", [])
        if not isinstance(pairs, list):
            report.check(False, f"listener expected_pairs is not a list: {case}")
            continue
        for pair in pairs:
            report.check(
                isinstance(pair, list)
                and len(pair) == 2
                and all(str(item) in ids for item in pair),
                f"listener pair references an unknown message: {pair}",
            )
    return report


def eval_answer_dataset() -> EvalReport:
    """Validate the expanded factual-answer dataset shape."""
    report = EvalReport(name="answers/dataset")
    cases = load_cases("answers.yaml")
    report.check(len(cases) >= 70, f"expected >= 70 answer cases, got {len(cases)}")
    for case in cases:
        report.check(bool(case.get("id")), f"answer case lacks id: {case}")
        report.check(
            bool(case.get("question")) or case.get("expected_mode") == "abstain",
            f"answer case lacks question: {case}",
        )
        report.check(
            isinstance(case.get("must_include"), list),
            f"answer case must_include is not a list: {case}",
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


def _conflict_evidence(case: dict[str, object]) -> list[Evidence]:
    """Build Evidence items from a conflicts.yaml case."""
    raw_evidence = case.get("evidence")
    items = []
    for index, raw in enumerate(raw_evidence if isinstance(raw_evidence, list) else []):
        assert isinstance(raw, dict)
        raw_authority = raw.get("authority", 0)
        authority = int(raw_authority) if isinstance(raw_authority, (int, float)) else 0
        items.append(
            Evidence(
                source_id=f"ev-{case.get('id', index)}-{index}",
                label="Grup" if authority < 100 else "Q&A",
                text=str(raw.get("text", "")),
                authority=authority,
                similarity=0.6,
            )
        )
    return items


def eval_conflicts() -> EvalReport:
    """Conflict behaviour (spec §42), verified offline.

    The gate's deterministic half: a model "insufficient" verdict always
    abstains, and the evidence handed to the model carries each source's
    authority intact (the prompt tells the model to prefer higher authority).
    """
    report = EvalReport(name="conflicts/gate")
    for case in load_cases("conflicts.yaml"):
        assert isinstance(case, dict)
        case_id = str(case.get("id", "?"))
        expected = case.get("expected") or {}
        assert isinstance(expected, dict)
        generator = FakeGenerator(GenerationOutput(status="insufficient"))
        service = AnswerService(
            retrieval=RetrievalService(
                embedder=FakeEmbedder(),
                vectors=FakeVectorStore(),
                lexical=FakeLexicalIndex(),
            ),
            generator=generator,
            answers=InMemoryBotAnswerRepository(),
            clock=FrozenClock(NOW),
        )
        question = str(case.get("question", ""))
        retrieved = RetrievedEvidence(messages=_conflict_evidence(case))
        outcome = asyncio.run(service.decide(question, retrieved))
        if "status" in expected:
            report.check(
                outcome.mode is AnswerMode.ABSTENTION,
                f"{case_id}: expected abstention on insufficient, got {outcome.mode}",
            )
        if "use_authority" in expected:
            request = generator.requests[-1]
            wanted_value = expected.get("use_authority")
            wanted = int(wanted_value) if isinstance(wanted_value, (int, float)) else 0
            carried = [item.authority for item in request.evidence]
            report.check(
                wanted in carried,
                f"{case_id}: authority {wanted} not carried to the model: {carried}",
            )
            strongest = max(request.evidence, key=lambda item: item.authority)
            report.check(
                wanted == strongest.authority,
                f"{case_id}: expected {wanted} to be the highest authority",
            )
    return report


def _correction_flow(
    answers: InMemoryBotAnswerRepository,
) -> tuple[FeedbackService, InMemoryQAItemRepository, InMemoryQAVersionRepository]:
    """Wire a FeedbackService to in-memory fakes for the corrections eval."""
    items = InMemoryQAItemRepository()
    versions = InMemoryQAVersionRepository()
    feedback = InMemoryFeedbackRepository()
    service = FeedbackService(
        answers=answers,
        feedback=feedback,
        qa_items=items,
        qa_versions=versions,
        conversations=InMemoryConversationRepository(),
        commits=InMemoryCorrectionCommitStore(
            items,
            versions,
            InMemoryQAEvidenceRepository(),
            feedback,
        ),
        clock=FrozenClock(NOW),
    )
    return service, items, versions


def eval_corrections() -> EvalReport:
    """Correction-flow eval (spec §43): six deterministic scenarios, 100% pass.

    Every case is an in-process flow over fakes: no model, no network. The
    YAML set documents the expected case list; the flow is verified in code
    because each scenario is a sequence of service calls, not static data.
    """
    report = EvalReport(name="corrections/flow")
    cases = load_cases("corrections.yaml")
    report.check(len(cases) == 6, f"expected 6 correction cases, got {len(cases)}")
    question = "Com es demana l'equipament?"

    async def run() -> None:
        # 1. auto answer -> correction -> approve
        answers = InMemoryBotAnswerRepository()
        await answers.add(
            BotAnswer(
                id="ans:m1",
                conversation_id="-100",
                question=question,
                answer="Resposta antiga.",
                answer_mode=AnswerMode.DIRECT_QA,
                created_at=NOW,
                user_message_id="m1",
            )
        )
        service, items, versions = _correction_flow(answers)
        started = await service.start("ans:m1", "telegram:reporter")
        assert started is not None
        await service.propose(
            started.id, "La llista la passa l'entrenador.", "telegram:reporter"
        )
        version = await service.approve(started.id, ReviewAction.APPROVE_GLOBAL)
        report.check(
            version is not None
            and version.authority == 100
            and version.origin == "human_approved",
            "case 1 (auto answer): approval did not create an authoritative version",
        )

        # 2. web answer -> local override (fresh fakes, web-seeded item)
        answers = InMemoryBotAnswerRepository()
        await answers.add(
            BotAnswer(
                id="ans:m1",
                conversation_id="-100",
                question=question,
                answer="Resposta antiga.",
                answer_mode=AnswerMode.DIRECT_QA,
                created_at=NOW,
                user_message_id="m1",
            )
        )
        service, items, versions = _correction_flow(answers)
        key = canonical_key_for(question)
        qa_id = f"qa-{key}"
        await items.add(
            QAItem(
                id=qa_id,
                canonical_key=key,
                canonical_question=question,
                status=QAStatus.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
                current_version_id="qav-web-1",
            )
        )
        await versions.add(
            QAVersion(
                id="qav-web-1",
                qa_id=qa_id,
                answer="Resposta del web.",
                authority=90,
                origin="web_seed",
                created_at=NOW,
            )
        )
        started = await service.start("ans:m1", "telegram:reporter")
        assert started is not None
        await service.propose(started.id, "Resposta corregida.", "telegram:reporter")
        override = await service.approve(started.id, ReviewAction.APPROVE_GLOBAL)
        report.check(
            override is not None
            and override.supersedes_version_id == "qav-web-1"
            and override.qa_id == qa_id,
            "case 2 (web override): approval did not supersede the web version",
        )
        report.check(
            await versions.get("qav-web-1") is not None,
            "case 2 (web override): the superseded web version was deleted",
        )

        # 3. correction -> reject (a fresh pending proposal)
        reject_answers = InMemoryBotAnswerRepository()
        await reject_answers.add(
            BotAnswer(
                id="ans:reject",
                conversation_id="-100",
                question=question,
                answer="Resposta rebutjada.",
                answer_mode=AnswerMode.DIRECT_QA,
                created_at=NOW,
                user_message_id="reject",
            )
        )
        reject_service, _, _ = _correction_flow(reject_answers)
        reject_started = await reject_service.start("ans:reject", "telegram:reporter")
        assert reject_started is not None
        await reject_service.propose(
            reject_started.id, "proposta rebutjada", "telegram:reporter"
        )
        rejected = await reject_service.reject(reject_started.id)
        report.check(
            rejected is not None and rejected.status.value == "rejected",
            "case 3 (reject): the proposal was not rejected",
        )

        # 4. correction -> admin edit -> approve
        answers = InMemoryBotAnswerRepository()
        await answers.add(
            BotAnswer(
                id="ans:m2",
                conversation_id="-100",
                question=question,
                answer="Resposta antiga.",
                answer_mode=AnswerMode.DIRECT_QA,
                created_at=NOW,
                user_message_id="m2",
            )
        )
        service, items, versions = _correction_flow(answers)
        started = await service.start("ans:m2", "telegram:reporter")
        assert started is not None
        await service.propose(started.id, "proposta del reporter", "telegram:reporter")
        await service.admin_edit(started.id, "text editat per l'admin")
        edited = await service.approve(started.id, ReviewAction.APPROVE_GLOBAL)
        report.check(
            edited is not None and edited.answer == "text editat per l'admin",
            "case 4 (admin edit): the edited text did not win",
        )

        # 5+6. two corrections in sequence; the newest stays current
        answers = InMemoryBotAnswerRepository()
        for index in (3, 4):
            await answers.add(
                BotAnswer(
                    id=f"ans:m{index}",
                    conversation_id="-100",
                    question=question,
                    answer=f"Resposta antiga {index}.",
                    answer_mode=AnswerMode.DIRECT_QA,
                    created_at=NOW,
                    user_message_id=f"m{index}",
                )
            )
        service, items, versions = _correction_flow(answers)
        first = await service.start("ans:m3", "telegram:reporter")
        assert first is not None
        await service.propose(first.id, "Primera correcció.", "telegram:reporter")
        first_version = await service.approve(first.id, ReviewAction.APPROVE_GLOBAL)
        second = await service.start("ans:m4", "telegram:reporter")
        assert second is not None
        await service.propose(second.id, "Segona correcció.", "telegram:reporter")
        second_version = await service.approve(second.id, ReviewAction.APPROVE_GLOBAL)
        assert first_version is not None and second_version is not None
        report.check(
            second_version.qa_id == first_version.qa_id
            and second_version.supersedes_version_id == first_version.id,
            "case 5 (sequential): the second correction did not supersede the first",
        )
        item = await items.get(second_version.qa_id)
        report.check(
            item is not None and item.current_version_id == second_version.id,
            "case 6 (newest wins): the current version is not the last approved one",
        )
        report.check(
            await versions.get(first_version.id) is not None,
            "case 6 (newest wins): the earlier version was destroyed",
        )

    asyncio.run(run())
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
            update={
                "source_anchor": "qa-y",
                "question": "Quan?",
                "status": "in_review",
            }
        )
        created, skipped, _, _, _ = await service.seed_qa([published, review])
        report.check(
            created == 2 and skipped == 0, f"expected 2 created, got {created}"
        )
        item = await items.get_by_canonical_key(canonical_key_for(published.question))
        report.check(
            item is not None and item.current_version_id is not None,
            "published item has no current version",
        )
        review_item = await items.get_by_canonical_key(
            canonical_key_for(review.question)
        )
        report.check(
            review_item is not None and review_item.status.value == "under_review",
            "in-review entry is not under_review",
        )
        report.check(
            await sources.get(source_instance_id("web_seed", published.source_url))
            is not None,
            "connector-declared seed source not created",
        )

    asyncio.run(run())
    return report


def run_offline() -> list[EvalReport]:
    """Run every offline eval."""
    return [
        eval_trigger(),
        eval_abstention_set(),
        eval_classifier_dataset(),
        eval_classifier_splits(),
        eval_retrieval_dataset(),
        eval_listener_dataset(),
        eval_answer_dataset(),
        eval_citation_format(),
        eval_seed_versioning(),
        eval_conflicts(),
        eval_corrections(),
    ]


# --- Live evals ------------------------------------------------------------


def _internal_headers() -> dict[str, str]:
    from knowledge_bot.infrastructure.settings import Settings

    return {"X-Internal-Key": Settings().internal_admin_key}


def eval_live_retrieval(base_url: str, *, reindex: bool = False) -> EvalReport:
    """Measure retrieval recall against the deployed index.

    The eval asks the Worker to retrieve for each query and checks that the
    expected anchor's version id comes back. Reindexing is opt-in: it re-embeds
    every record, which is the largest single draw on the Workers AI quota and
    is not what this eval measures.

    Args:
        base_url: The deployed Worker base URL.
        reindex: When true, rebuild the index and wait for it to settle first.

    Returns:
        The eval report.
    """
    report = EvalReport(name="retrieval/recall@5")
    cases = [*load_cases("retrieval.yaml"), *load_cases("retrieval_synthetic.yaml")]
    try:
        if reindex:
            httpx.post(
                f"{base_url}/internal/reindex",
                headers=_internal_headers(),
                timeout=600.0,
            )
            # Vectorize indexing is eventually consistent: let upserts settle.
            time.sleep(60)
        probe = httpx.post(
            f"{base_url}/internal/retrieve",
            headers=_internal_headers(),
            json={"queries": [case["query"] for case in cases]},
            timeout=600.0,
        )
        probe.raise_for_status()
    except httpx.HTTPError as error:
        report.check(False, f"live retrieval probe failed: {_explain(error)}")
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


def _explain(error: httpx.HTTPError) -> str:
    """Return a readable reason for a failed eval request."""
    response = getattr(error, "response", None)
    if response is not None:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        if detail:
            return str(detail)
    return str(error)


def _terms(value: object) -> list[str]:
    """Return a case field as a list of strings."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _eval_answer(base_url: str, case: dict[str, object], report: EvalReport) -> None:
    """Ask one question live and assert deterministic answer and citation rules."""
    question = str(case["question"])
    try:
        response = httpx.post(
            f"{base_url}/internal/eval/answer",
            headers=_internal_headers(),
            json={"question": question},
            timeout=600.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as error:
        report.check(False, f"{question!r}: {_explain(error)}")
        return
    if not isinstance(payload, dict):
        report.check(False, f"{question!r}: malformed response")
        return
    mode = str(payload.get("mode"))
    answer = str(payload.get("answer", ""))
    expected_mode = case.get("expected_mode")
    if expected_mode is not None:
        report.check(
            mode == str(expected_mode),
            f"{question!r}: mode {mode!r} != {expected_mode!r}",
        )
    citations = [
        item for item in payload.get("citations") or [] if isinstance(item, dict)
    ]
    if mode != "abstention":
        ids = _terms(payload.get("source_ids"))
        stray = [sid for sid in ids if sid not in _terms(payload.get("evidence_ids"))]
        report.check(not stray, f"{question!r}: unsupported sources cited: {stray}")
        report.check(bool(ids), f"{question!r}: an answer must cite a source")
        for citation in citations:
            url = str(citation.get("url") or "")
            author = citation.get("author")
            if url:
                report.check(
                    "#" in url, f"{question!r}: web citation lacks an anchor: {url!r}"
                )
                report.check(
                    bool(citation.get("date")),
                    f"{question!r}: web citation lacks a date",
                )
            else:
                report.check(
                    bool(author and citation.get("date")),
                    f"{question!r}: citation lacks a URL and an author+date",
                )
        for term in _terms(case.get("must_include")):
            if term.lower() not in answer.lower():
                report.warn(f"{question!r}: missing term {term!r} (advisory)")
        for claim in _terms(case.get("must_not_claim")):
            report.check(
                claim.lower() not in answer.lower(),
                f"{question!r}: answer claims {claim!r}",
            )


def eval_live_answers(base_url: str) -> EvalReport:
    """Check that live answers are correct, sourced, and grounded (spec §46)."""
    report = EvalReport(name="answers/live")
    for case in load_cases("answers.yaml"):
        _eval_answer(base_url, case, report)
    return report


def eval_live_abstention(base_url: str) -> EvalReport:
    """Check that unknown questions abstain instead of inventing (spec §41)."""
    report = EvalReport(name="abstention/live")
    for case in [
        *load_cases("abstention.yaml"),
        *load_cases("abstention_synthetic.yaml"),
    ]:
        _eval_answer(base_url, {**case, "expected_mode": "abstention"}, report)
    return report


def run_live(base_url: str, *, reindex: bool = False) -> list[EvalReport]:
    """Run every live eval."""
    return [
        eval_live_retrieval(base_url, reindex=reindex),
        eval_live_answers(base_url),
        eval_live_abstention(base_url),
    ]


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
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Rebuild the vector index first (opt-in: burns the AI quota)",
    )
    args = parser.parse_args(argv)

    if args.mode == "live" and (
        not args.base_url or os.getenv("ALLOW_CLOUDFLARE_LIVE_TESTS") != "1"
    ):
        parser.error("live evals require --base-url and ALLOW_CLOUDFLARE_LIVE_TESTS=1")
    reports = (
        run_offline()
        if args.mode == "offline"
        else run_live(args.base_url, reindex=args.reindex)
    )
    print(f"# {args.mode.capitalize()} evals\n")
    for report in reports:
        print(report.render())
        print()
    failed = [report for report in reports if not report.ok]
    print(f"Suites passed: {len(reports) - len(failed)}/{len(reports)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
