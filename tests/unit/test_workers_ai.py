# SPDX-License-Identifier: MIT
"""Unit tests for the Workers AI adapters and their privacy-safe call logging."""

import asyncio
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

import pytest

from knowledge_bot.domain.errors import (
    InvalidModelOutputError,
    ModelUnavailableError,
)
from knowledge_bot.infrastructure.cloudflare.workers_ai import (
    WorkersAIEmbedder,
    WorkersAIGenerator,
)
from knowledge_bot.infrastructure.logging import _RESERVED_RECORD_FIELDS
from knowledge_bot.ports.generator import EvidenceItem, GenerationRequest


class _Runner:
    """A Workers AI stand-in returning a canned result or raising."""

    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        """Store the canned outcome and the delay applied to every call."""
        self.result = result
        self.error = error
        self.delay = 0.0
        self.inputs: dict[str, object] = {}

    async def run(self, model: str, inputs: dict[str, object]) -> object:
        """Return or raise the canned outcome."""
        del model
        self.inputs = inputs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass(frozen=True, slots=True)
class _Call:
    """One captured Workers AI call log record, reduced to asserted fields."""

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
    """Capture the Workers AI call log records emitted during the test."""
    captured: list[_Call] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(
                _Call(
                    message=record.getMessage(),
                    operation=cast("str", getattr(record, "operation", "")),
                    model=cast("str", getattr(record, "model", "")),
                    characters=cast("int", getattr(record, "characters", 0)),
                    duration_ms=cast("float", getattr(record, "duration_ms", 0.0)),
                    error=cast("str", getattr(record, "error", "")),
                    extra={
                        key: value
                        for key, value in record.__dict__.items()
                        if key not in _RESERVED_RECORD_FIELDS
                    },
                )
            )

    handler = _Capture()
    target = logging.getLogger("knowledge_bot.ai")
    previous_level = target.level
    target.setLevel(logging.DEBUG)
    target.addHandler(handler)
    yield captured
    target.removeHandler(handler)
    target.setLevel(previous_level)


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


async def test_generator_raises_on_unparseable_output() -> None:
    """Output that is not JSON is a provider failure, not an abstention."""
    runner = _Runner({"choices": [{"message": {"content": "not json"}}]})
    generator = WorkersAIGenerator(runner, "chat-model", timeout_seconds=1.0)

    with pytest.raises(InvalidModelOutputError) as raised:
        await generator.generate(
            GenerationRequest(
                question="q",
                evidence=[
                    EvidenceItem(
                        source_id="s1", text="t", label="l", authority=1, similarity=0.5
                    )
                ],
            )
        )

    assert raised.value.code == "no_json"


async def test_generator_bounds_the_output_budget() -> None:
    """The reasoning model gets a token cap, without a response_format."""
    runner = _Runner(
        {"choices": [{"message": {"content": '{"status": "insufficient"}'}}]}
    )
    generator = WorkersAIGenerator(runner, "chat-model", 1.0, 1024)

    await generator.generate(GenerationRequest(question="q", evidence=[]))

    assert runner.inputs["max_tokens"] == 1024
    assert "format" not in runner.inputs


async def test_generator_raises_domain_error_on_failure() -> None:
    """A failing generation call becomes ModelUnavailableError."""
    runner = _Runner(error=RuntimeError("boom"))
    generator = WorkersAIGenerator(runner, "chat-model", timeout_seconds=1.0)

    with pytest.raises(ModelUnavailableError):
        await generator.generate(GenerationRequest(question="q", evidence=[]))


async def test_generator_raises_when_the_reply_has_no_content() -> None:
    """An empty reply is a provider failure, not a model abstention."""
    runner = _Runner({"choices": [{"message": {"content": ""}}]})
    generator = WorkersAIGenerator(runner, "chat-model", timeout_seconds=1.0)

    with pytest.raises(InvalidModelOutputError) as raised:
        await generator.generate(
            GenerationRequest(
                question="q",
                evidence=[
                    EvidenceItem(
                        source_id="s1", text="t", label="l", authority=1, similarity=0.5
                    )
                ],
            )
        )

    assert raised.value.code == "missing_content"


async def test_generator_raises_on_a_schema_invalid_reply() -> None:
    """A JSON object that does not match the contract is a provider failure."""
    runner = _Runner({"choices": [{"message": {"content": '{"status": "answered"}'}}]})
    generator = WorkersAIGenerator(runner, "chat-model", timeout_seconds=1.0)

    with pytest.raises(InvalidModelOutputError) as raised:
        await generator.generate(
            GenerationRequest(
                question="q",
                evidence=[
                    EvidenceItem(
                        source_id="s1", text="t", label="l", authority=1, similarity=0.5
                    )
                ],
            )
        )

    assert raised.value.code == "schema_validation"


async def test_the_error_never_carries_the_model_output() -> None:
    """The code is safe to export; the text it came from is not."""
    runner = _Runner(
        {"choices": [{"message": {"content": "secret internal reasoning here"}}]}
    )
    generator = WorkersAIGenerator(runner, "chat-model", timeout_seconds=1.0)

    with pytest.raises(InvalidModelOutputError) as raised:
        await generator.generate(
            GenerationRequest(
                question="q",
                evidence=[
                    EvidenceItem(
                        source_id="s1", text="t", label="l", authority=1, similarity=0.5
                    )
                ],
            )
        )

    assert "secret internal reasoning" not in str(raised.value)


async def test_no_think_switch_is_appended_to_the_user_message() -> None:
    """``/no_think`` reaches the model, and only when asked for.

    Qwen3 thinks by default and the deliberation does not fit the 35 s adapter
    deadline, so the switch has to be in the user message. The test asserts it
    is absent by default, because a stray token in the prompt changes the
    answer for every other model.
    """
    plain = _Runner(
        {"choices": [{"message": {"content": '{"status": "insufficient"}'}}]}
    )
    with_switch = _Runner(
        {"choices": [{"message": {"content": '{"status": "insufficient"}'}}]}
    )
    request = GenerationRequest(question="q", evidence=[])

    await WorkersAIGenerator(plain, "chat-model", timeout_seconds=1.0).generate(request)
    await WorkersAIGenerator(
        with_switch, "chat-model", timeout_seconds=1.0, no_think=True
    ).generate(request)

    def user_message(runner: _Runner) -> str:
        sent = cast("list[dict[str, str]]", runner.inputs["messages"])
        return str(sent[-1]["content"])

    assert "/no_think" not in user_message(plain)
    assert user_message(with_switch).rstrip().endswith("/no_think")
