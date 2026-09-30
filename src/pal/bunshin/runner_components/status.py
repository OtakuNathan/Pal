from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Status:
    blocked_summary: str = ""
    blocked_kind: str = ""

    def block(self, summary: str) -> None:
        self.blocked_summary = summary

    def classify_blocker(self, kind: str) -> None:
        self.blocked_kind = kind
