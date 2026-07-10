from __future__ import annotations

from dataclasses import dataclass

from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider
from pal.minion.service import TaskingService


@dataclass
class TaskingPromptFragmentProvider(PromptFragmentProvider):
    service: TaskingService | None = None
    manager: object | None = None
    provider_id: str = "minion.prompt.default"
    module_id: str = "minion"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context, self.manager
        fragments: list[PromptFragment] = [
            PromptFragment(
                section="task_flow",
                title="Minion Usage",
                content=(
                    "Minion Usage: use Minion V2 for professional, asynchronous, bounded work that benefits from durable "
                    "contract planning, isolated implementation, adversarial verification, or background execution. "
                    "Do not use Minion for casual chat, simple Q&A, one-call runtime actions, memory/preference correction, "
                    "or work that needs continuous user back-and-forth.\n\n"
                    "Workflow path:\n"
                    "1. Start normal work with `op_minion_start_workflow(operation=\"new_requirement\", ...)`. Preserve the "
                    "user's atomic requirements, constraints, workspace, read-only references, and explicit research mode.\n"
                    "2. The manager creates and reviews an immutable Architecture Contract, then sends the compiled review "
                    "card to the active channel. Do not claim execution has started while it is waiting for human review.\n"
                    "3. Accept, edit, or reject only through `op_minion_submit_human_decision`; an edit creates a new "
                    "architecture revision. Accepted contracts compile into an execution DAG automatically.\n\n"
                    "Use `intro_minion_workflow_status` for all progress reports. Report its single current phase, active "
                    "worker or node, blocker, liveness, and next legal action; never infer progress from old chat, logs, "
                    "worker-local state, milestones, cursors, or checkpoints. Use `op_minion_control_workflow` for asynchronous "
                    "pause/cancel, `op_minion_resume_workflow` only for a deliberately paused workflow, and "
                    "`op_minion_archive_workflow` only after it reaches a terminal state. Pal remains the user-facing "
                    "coordinator and must not invoke manager/admin lease, tick, spawn, or outbox internals."
                ),
                priority=89,
                metadata={"block_id": "minion_usage"},
            )
        ]
        if self.service is not None:
            fragments.append(
                PromptFragment(
                    section="runtime",
                    title="Tasking Context",
                    content=f"Issued work orders: {len(self.service.issued_work_orders)}",
                    priority=45,
                    metadata={"block_id": "tasking_context"},
                )
            )
        return fragments
