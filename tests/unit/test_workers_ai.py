# SPDX-License-Identifier: MIT
"""Unit tests for the Workers AI adapters and their privacy-safe call logging."""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

import pytest
from loguru import logger

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.infrastructure.cloudflare.workers_ai import (
    WorkersAIEmbedder,
    WorkersAIGenerator,
)
from knowledge_bot.ports.generator import EvidenceItem, GenerationRequest


class _Runner:
    """A Workers AI stand-in returning a canned result or raising."""

    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        """Store the canned outcome and the delay applied to every call."""
        self.result = result
        self.error = error
        self.delay = 0.0

    async def run(self, model: str, inputs: dict[str, object]) -> object:
        """Return or raise the canned outcome."""
        del model, inputs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass(frozen=True, slots=True)
class _Call:
    """One captured Workers AI call log line, reduced to asserted fields."""

    message: str
    operation: str
    model: str
    characters: int
    duration_ms: float
    error: str
    extra: dict[str, object]

    def text(self) -> str:
        """Return the line's contextual fields as JSON for leak assertions."""
        return json.dumps(self.extra, default=str)


@pytest.fixture
def calls() -> Iterator[list[_Call]]:
    """Capture the Workers AI call log lines emitted during the test."""
    captured: list[_Call] = []

    def sink(raw: str) -> None:
        record = cast("dict[str, object]", raw.record)  # ty: ignore[unresolved-attribute]
        extra = cast("dict[str, object]", record["extra"])
        captured.append(
            _Call(
                message=cast("str", record["message"]),
                operation=cast("str", extra.get("operation", "")),
                model=cast("str", extra.get("model", "")),
                characters=cast("int", extra.get("characters", 0)),
                duration_ms=cast("float", extra.get("duration_ms", 0.0)),
                error=cast("str", extra.get("error", "")),
                extra=extra,
            )
        )

    sink_id = logger.add(sink, level="DEBUG")
    yield captured
    logger.remove(sink_id)


def _only(captured: list[_Call], message: str) -> _Call:
    """Return the single captured line with the given message."""
    matching = [line for line in captured if line.message == message]
    assert len(matching) == 1, f"expected one {message} line, got {len(matching)}"
    return matching[0]


async def test_embedder_returns_rows() -> None:
    """A well-formed Workers AI response is converted to float rows."""
    runner = _Runner({"data": [[0.1, 0.2], [0.3, 0.4]]})
    embedder = WorkersAIEmbedder(runner, "embed-model", timeout_seconds=1.0)

    assert await embedder.embed(["a", "bb"]) == [[0.1, 0.2], [0.3, 0.4]]


async def test_embedder_raises_domain_error_on_failure() -> None:
    """Any runner failure becomes ModelUnavailableError, never a raw error."""
    runner = _Runner(error=RuntimeError("boom"))
    embedder = WorkersAIEmbedder(runner, "embed-model", timeout_seconds=1.0)

    with pytest.raises(ModelUnavailableError):
        await embedder.embed(["a"])


async def test_embedder_raises_domain_error_on_timeout(calls: list[_Call]) -> None:
    """A slow runner is bounded by the adapter deadline and logged as such."""
    runner = _Runner({"data": [[0.1]]})
    runner.delay = 5.0
    embedder = WorkersAIEmbedder(runner, "embed-model", timeout_seconds=0.05)

    with pytest.raises(ModelUnavailableError):
        await embedder.embed(["a"])

    line = _only(calls, "workers_ai_call_failed")
    assert line.error == "TimeoutError"


async def test_embedder_rejects_wrong_row_count() -> None:
    """A short response is a model failure, not a partial result."""
    runner = _Runner({"data": [[0.1]]})
    embedder = WorkersAIEmbedder(runner, "embed-model", timeout_seconds=1.0)

    with pytest.raises(ModelUnavailableError):
        await embedder.embed(["a", "b"])


async def test_call_logging_records_size_and_duration_without_text(
    calls: list[_Call],
) -> None:
    """A successful call logs model, size, and duration but never the text."""
    runner = _Runner({"data": [[0.1, 0.2]]})
    embedder = WorkersAIEmbedder(runner, "embed-model", timeout_seconds=1.0)

    await embedder.embed(["secret question"])

    line = _only(calls, "workers_ai_call_completed")
    assert line.operation == "embedding"
    assert line.model == "embed-model"
    assert line.characters == len("secret question")
    assert line.duration_ms >= 0.0
    assert "secret question" not in line.text()


async def test_call_logging_records_failure_without_text(calls: list[_Call]) -> None:
    """A failed call logs the error class and still never the payload."""
    runner = _Runner(error=RuntimeError("provider detail with secret"))
    embedder = WorkersAIEmbedder(runner, "embed-model", timeout_seconds=1.0)

    with pytest.raises(ModelUnavailableError):
        await embedder.embed(["secret question"])

    line = _only(calls, "workers_ai_call_failed")
    assert line.error == "RuntimeError"
    assert "secret question" not in line.text()
    assert "provider detail" not in line.text()


async def test_generator_reports_insufficient_on_unparseable_output() -> None:
    """Model output that is not JSON degrades to insufficient, not a crash."""
    runner = _Runner({"choices": [{"message": {"content": "not json"}}]})
    generator = WorkersAIGenerator(runner, "chat-model", timeout_seconds=1.0)

    output = await generator.generate(
        GenerationRequest(
            question="q",
            evidence=[EvidenceItem(source_id="s1", text="t", label="l", authority=1)],
        )
    )

    assert output.status == "insufficient"


async def test_generator_raises_domain_error_on_failure() -> None:
    """A failing generation call becomes ModelUnavailableError."""
    runner = _Runner(error=RuntimeError("boom"))
    generator = WorkersAIGenerator(runner, "chat-model", timeout_seconds=1.0)

    with pytest.raises(ModelUnavailableError):
        await generator.generate(GenerationRequest(question="q", evidence=[]))
