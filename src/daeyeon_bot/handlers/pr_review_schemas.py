"""Pydantic v2 schema for the JSON Claude must produce per `contracts/claude-review-output.md` §1.

`extra="forbid"` is the load-bearing knob: a hallucinated key like
`"approve": true` must NOT silently sneak into a future API call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class InlineComment(BaseModel):
    """One inline review comment anchored to a line in the post-change diff."""

    model_config = {"extra": "forbid"}

    path: str = Field(min_length=1, max_length=512)
    line: int = Field(ge=1)
    side: Literal["RIGHT", "LEFT"] = "RIGHT"
    start_line: int | None = Field(default=None, ge=1)
    body: str = Field(min_length=1, max_length=8000)


class ReviewOutput(BaseModel):
    """Top-level Claude output: a Summary + zero or more inline comments."""

    model_config = {"extra": "forbid"}

    summary: str = Field(min_length=1, max_length=2500)
    comments: list[InlineComment] = Field(default_factory=list, max_length=200)


__all__ = ["InlineComment", "ReviewOutput"]
