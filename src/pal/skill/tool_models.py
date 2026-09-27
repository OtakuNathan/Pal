"""Explicit skill patch contract shared by tool validation and direct callers."""
from __future__ import annotations

from pydantic import Field
from typing import Literal

from pal.execution.tool_facade import StrictToolModel


class SkillSTARPatch(StrictToolModel):
    # Omission preserves the current value; explicit empty strings clear it.
    situation: str = Field(default=None)
    task: str = Field(default=None)
    action: str = Field(default=None)
    result: str = Field(default=None)


class SkillPatch(StrictToolModel):
    title: str = Field(default=None, min_length=1, pattern=r"\S")
    summary: str = Field(default=None)
    manual_text: str = Field(default=None, min_length=1, pattern=r"\S")
    activation_terms: list[str] = Field(default=None)
    capability_refs: list[str] = Field(default=None)
    enabled: bool = Field(default=None)
    status: Literal["draft", "active", "disabled", "deprecated", "needs_review"] = Field(default=None)
    applicability_star: SkillSTARPatch = Field(default=None)
    use_when: str = Field(default=None)
    avoid_when: str = Field(default=None)
    sanitization_notes: list[str] = Field(default=None)
