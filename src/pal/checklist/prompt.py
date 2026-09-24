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
                    "Keep the active checklist small and concrete; its first unfinished item is the work cursor. "
                    "Use checklist_check for one completed phase or checklist_upsert to update several phases together. "
                    "Before marking the final phase complete, review the user's requirements against actual results and note omissions or unverified items; do not repeat completed checks. "
                    "The last checklist_check and a fully completed checklist_upsert automatically close the checklist. Use checklist_clear to cancel or replace work. "
                    "Batch checklist creation, updates, and checks for already-confirmed phases with independent useful tool calls in the same response instead of spending a separate round on bookkeeping. "
                    "For example, check a completed inspection alongside the next edit; wait for test results before checking verification complete. "
                    "Do not perform remaining work just to clear the checklist or repeat verification solely to close it. "
                    "Summarize from actual execution evidence, including omitted checks and unfinished work; the checklist "
                    "is not truth, evidence, or permission. Complex read-only investigations may use it when long-running "
                    "or resumable. Simple answers, routine edits, and Manager-owned Bunshin workflows need no extra checklist."
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
