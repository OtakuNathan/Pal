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
                    "Minion Usage: use Minion for professional, asynchronous, bounded work that benefits from a task ledger, "
                    "family/profile policy, milestones, artifacts, review gates, or module-scoped execution. "
                    "Do not use Minion for casual chat, simple Q&A, one-call runtime actions, memory/preference correction, "
                    "or work that needs continuous user back-and-forth.\n\n"
                    "Default delegation path:\n"
                    "1. Search existing durable tasks with `minion_task_search`.\n"
                    "2. Reuse the matching `task_id`, or create one with `minion_task_create` so the profile family/domain is bound at the task layer.\n"
                    "3. Dispatch with `minion_dispatch_workflow(task_id=...)`. Do not pass profile selectors to dispatch; the manager derives planner, reviewer, and executor roles from the task family/profile configuration.\n\n"
                    "Pal remains the user-facing coordinator: shape requirements before dispatch, ask the user when approval or clarification is needed, inspect work-order/task facts before reporting status, and summarize completed artifacts without exposing internal management ids unless the user asks. "
                    "For active work, inspect `minion_list`, `minion_read`, `minion_work_order_search`, and `minion_work_order_read`; do not infer progress from chat history or old logs. "
                    "For stale or interrupted work, use work-order control tools such as `minion_recover_work_order`, `minion_resume_work_order`, then `minion_tick_parent_dag` when continuing a parent DAG."
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
