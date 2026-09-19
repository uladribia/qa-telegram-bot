# SPDX-License-Identifier: MIT
"""Seed Q&A contracts (spec §7.1)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SeedQA(BaseModel):
    """A Q&A entry extracted from the published knowledge site."""

    source_url: str
    section: str
    question: str
    answer: str
    status: Literal["published", "in_review"]
    retrieved_at: datetime
    source_anchor: str | None = None
