from __future__ import annotations

from dataclasses import dataclass

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
                    "Keep the active checklist small, concrete, and user-readable (it serves as a progress summary for the user); "
                    "batch updates (`complete_checklist_step` / `upsert_checklist`) with independent tool calls instead of "
                    "spending a separate round on bookkeeping. Completing the last step will automatically close the checklist; "
                    "use `close_checklist` to cancel or replace work.\n"
                    "Before finishing, review the user's requirements against actual results without repeating completed checks. "
                    "Do not perform remaining work just to clear the checklist. Summarize from actual execution evidence "
                    "(the checklist is a work cursor, not truth, evidence, or permission). Simple answers, routine edits, and "
                    "Bunshin workflows need no extra checklist."
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
        remaining = ([str(item["step"]) for item in snapshot.plan if item["status"] != "completed"]
                     if snapshot is not None else [])
        fragments.append(
            PromptFragment(
                section="task_flow",
                title="Checklist State",
                content=f"Checklist: {len(remaining)} unfinished steps.",
                priority=10,
                metadata={
                    "module_id": self.module_id,
                    "kind": "checklist_state",
                    "prompt_target": "runtime_reminder",
                    "block_id": "checklist_state",
                    "history_state": {"active": snapshot is not None, "remaining": remaining},
                },
            )
        )
        return fragments


__all__ = ["ChecklistPromptFragmentProvider"]
