# SPDX-License-Identifier: MIT
"""Narrow transactional ports for semantic knowledge changes."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from knowledge_bot.domain.entities import Feedback, QAEvidence, QAItem, QAVersion


@dataclass(frozen=True, slots=True)
class ApproveCorrectionCommand:
    """All semantic writes required to approve one correction."""

    feedback: Feedback
    item: QAItem
    version: QAVersion
    evidence: QAEvidence


@runtime_checkable
class CorrectionCommitStore(Protocol):
    """Atomically commits one correction decision."""

    async def approve(self, command: ApproveCorrectionCommand) -> QAVersion:
        """Persist the version, pointer, evidence, and feedback decision."""
        ...

    async def reject(self, feedback: Feedback) -> Feedback:
        """Persist one rejection decision."""
        ...
