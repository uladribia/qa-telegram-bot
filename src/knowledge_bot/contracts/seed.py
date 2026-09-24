# SPDX-License-Identifier: MIT
"""Seed Q&A contracts (spec §7.1)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SeedQA(BaseModel):
    """A Q&A entry with connector-declared source provenance."""

    source_url: str
    source_kind: str = "web_seed"
    source_authority: int = 90
    section: str
    question: str
    answer: str
    status: Literal["published", "in_review"]
    retrieved_at: datetime
    source_anchor: str | None = None
