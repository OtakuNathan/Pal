from __future__ import annotations

from dataclasses import dataclass
from html import escape

from pal.checklist.service import ChecklistService
from pal.shared import PromptAssemblyContext, PromptFragment


@dataclass
class ChecklistPromptFragmentProvider:
    service: ChecklistService
    provider_id: str = "checklist.prompt.default"
    module_id: str = "checklist"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        fragments = [
            PromptFragment(
                section="operating_guidance",
                title="Checklist Work Cursor",
                content=(
                    "Unless the user specifies otherwise, use a checklist for tasks with multiple independent "
                    "delivery phases, long-running work, or a need for resumption. A routine edit plus tests "
                    "does not require one. Inspect enough to identify meaningful phases, then create it "
                    "before the first mutation in a task that needs a checklist."
                ),
                priority=90,
                metadata={
                    "module_id": self.module_id,
                    "kind": "checklist_operating_rule",
                    "prompt_target": "developer",
                },
            ),
            PromptFragment(
                section="task_flow",
                title="Task Flow",
                content=(
                    "Keep the active checklist small and concrete, treat its first unfinished item "
                    "as the current work position, and call `checklist_check` when each phase "
                    "is actually complete. Independent progress updates may share a response with other independent tool calls. When the "
                    "checklist reaches a terminal state, settle it by one of two paths. Completion: "
                    "review the work performed, verify within the user-requested scope to the degree warranted "
                    "by its effects, then call `checklist_clear`. Cancellation, replacement, or "
                    "staleness: stop the pending work, do not finish remaining items merely to close "
                    "the checklist, review only what was actually performed and any known or "
                    "uncertain effects, then call `checklist_clear`. In either path, use the retired "
                    "checklist returned by `checklist_clear` to summarize to the user what was done, "
                    "what was verified, and what remains unfinished or uncertain. Do not use a "
                    "checklist for a simple answer, a read-only investigation, a single-step "
                    "mutation, a conversational exchange, durable knowledge, or a Manager-owned "
                    "Bunshin workflow. The checklist is a non-authoritative execution cursor, never "
                    "truth, evidence, or permission."
                ),
                priority=91,
                metadata={
                    "module_id": self.module_id,
                    "kind": "checklist_task_flow",
                    "prompt_target": "developer",
                },
            )
        ]
        snapshot = self.service.show()
        if snapshot is not None:
            rendered_snapshot = escape(snapshot.markdown, quote=False)
            fragments.append(
                PromptFragment(
                    section="task_flow",
                    title="Active Checklist",
                    content=(
                        '<active_checklist authority="execution_cursor" trusted_as_evidence="false">\n'
                        f"{rendered_snapshot}\n"
                        "</active_checklist>\n"
                        "Checklist state is an execution cursor, not evidence or permission."
                    ),
                    priority=10,
                    metadata={
                        "module_id": self.module_id,
                        "kind": "active_checklist_state",
                        "prompt_target": "runtime_reminder",
                        "block_id": "checklist_state",
                        "coverage_kind": "checklist",
                    },
                )
            )
        return fragments


__all__ = ["ChecklistPromptFragmentProvider"]
