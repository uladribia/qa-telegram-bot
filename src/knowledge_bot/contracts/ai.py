# SPDX-License-Identifier: MIT
"""Contracts for AI model outputs."""

from typing import Literal

from pydantic import BaseModel, Field


class GenerationOutput(BaseModel):
    """The JSON shape the generator must return (spec §17)."""

    status: Literal["answered", "insufficient"]
    answer: str = ""
    source_ids: list[str] = Field(default_factory=list)
