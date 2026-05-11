from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider
from pal.minion.service import TaskingService


@dataclass
class TaskingPromptFragmentProvider(PromptFragmentProvider):
    service: TaskingService | None = None
    manager: object | None = None
    provider_id: str = "minion.prompt.default"
    module_id: str = "minion"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        observations = self._recent_observations()
        fragments: list[PromptFragment] = []
        if observations:
            fragments.append(
                PromptFragment(
                    section="runtime",
                    title="Recent Minion Completions",
                    content=_render_recent_observations(observations),
                    priority=45,
                    metadata={"block_id": "recent_minion_completions"},
                )
            )
        elif self.service is not None:
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

    def _recent_observations(self) -> list[dict[str, Any]]:
        getter = getattr(self.manager, "recent_minion_observations", None)
        if not callable(getter):
            return []
        try:
            return [dict(item) for item in list(getter(limit=5) or []) if isinstance(item, dict)]
        except Exception:
            return []


def _render_recent_observations(observations: list[dict[str, Any]]) -> str:
    lines = [
        "Recent minion terminal facts are already synchronized from manager push events.",
        "Use these facts before asking the user to poll minion status.",
    ]
    for item in observations[:5]:
        artifacts = [dict(artifact) for artifact in list(item.get("artifacts") or []) if isinstance(artifact, dict)]
        artifact_paths = [
            str(artifact.get("path") or artifact.get("relative_path") or "").strip()
            for artifact in artifacts[:3]
            if str(artifact.get("path") or artifact.get("relative_path") or "").strip()
        ]
        parts = [
            f"run={item.get('run_id') or '-'}",
            f"work_order={item.get('work_order_id') or '-'}",
            f"profile={item.get('profile') or '-'}",
            f"status={item.get('status') or '-'}",
            f"completed_at={item.get('completed_at') or '-'}",
        ]
        if artifact_paths:
            parts.append(f"artifacts={', '.join(artifact_paths)}")
        summary = str(item.get("summary") or "").strip()
        if summary:
            parts.append(f"summary={summary}")
        lines.append("- " + "; ".join(parts))
    return "\n".join(lines)
