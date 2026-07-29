"""Stable library response models; the standalone FastAPI service is not vendored."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ContentChunk(BaseModel):
    content: str
    page: int | str
    attachment_path: str | None = None


class ParsedFileResponse(BaseModel):
    filename: str
    chunks: list[ContentChunk] = Field(default_factory=list)

