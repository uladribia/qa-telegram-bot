# SPDX-License-Identifier: MIT
"""Shared value models used across every channel boundary."""

from pydantic import BaseModel, Field

from knowledge_bot.domain.enums import ProcessingStatus


class AttachmentRef(BaseModel):
    """Reference to an attachment; binary content is never downloaded in v1."""

    kind: str
    external_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    processing_status: ProcessingStatus = ProcessingStatus.UNPROCESSED


class SourceDescriptor(BaseModel):
    """Connector-declared identity and base authority for one source."""

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    authority: int = Field(ge=0, le=100)
