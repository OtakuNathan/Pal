from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Status:
    blocked_summary: str = ""
    blocked_kind: str = ""
    diagnostics: list[str] = field(default_factory=list)

    def block(self, summary: str) -> None:
        self.blocked_summary = summary

    def classify_blocker(self, kind: str) -> None:
        self.blocked_kind = kind
