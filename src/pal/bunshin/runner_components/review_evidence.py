from __future__ import annotations
from pal.shared.tool_protocol import ToolCallIR
from dataclasses import dataclass, field
from typing import Any
from pal.bunshin.scoped_execution import _review_tool_evidence_ref
from pal.shared import ToolExecutionResult
from pal.bunshin.runner_components.reporter import Reporter


@dataclass
class ReviewEvidence:
    reporter: Reporter
    review_tool_evidence_refs: list[dict[str, Any]] = field(default_factory=list)

    def record_review_tool_evidence(self, target_name: str, tool_call: ToolCallIR, result: ToolExecutionResult) -> None:
        evidence = _review_tool_evidence_ref(target_name, tool_call, result)
        if not evidence:
            return
        self.review_tool_evidence_refs.append(evidence)
        self.reporter.append_debug_log("review_tool_evidence_ref", evidence)

    def restore(self, records: list[dict[str, Any]]) -> None:
        self.review_tool_evidence_refs[:] = [dict(item) for item in records]
