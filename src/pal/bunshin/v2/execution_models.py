from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.v2.contracts import PermanentEffectError


@dataclass(frozen=True)
class ExecutionCompilation:
    epoch_id: str
    node_run_ids: tuple[str, ...]
    unit_node_ids: Mapping[str, str]
    sink_node_id: str


class DependencyIntegrationConflict(PermanentEffectError):
    """A dependency delta conflicts with the current immutable Candidate."""


@dataclass(frozen=True)
class NodeRunJournal:
    current_micro_plan: tuple[str, ...] = ()
    completed_checklist: tuple[str, ...] = ()
    files_inspected: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    known_failures: tuple[str, ...] = ()
    last_safe_point: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_micro_plan": list(self.current_micro_plan),
            "completed_checklist": list(self.completed_checklist),
            "files_inspected": list(self.files_inspected),
            "files_changed": list(self.files_changed),
            "open_questions": list(self.open_questions),
            "known_failures": list(self.known_failures),
            "last_safe_point": self.last_safe_point,
        }
