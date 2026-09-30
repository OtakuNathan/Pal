from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from pal.bunshin.prompt_adapter import prompt_view_from_pack as _prompt_view_from_pack
from pal.shared import BunshinInvocationPack
from pal.bunshin.runner_components.tool_session import ToolSession


@dataclass
class PromptContext:
    tool_session: ToolSession
    pack: BunshinInvocationPack
    runtime_root: Path

    def render_durable_role_context(self) -> str:
        binding = dict((self.pack.metadata or {}).get("bunshin_v2") or {})
        workspace = {
            **dict(self.pack.workspace or {}),
            "runtime_root": str(self.runtime_root),
            "invocation_id": self.pack.invocation_id,
            "bunshin_v2": binding,
        }
        if not str(binding.get("role") or ""):
            return ""
        from pal.bunshin.v2.work_items import render_work_item_context

        return render_work_item_context(workspace)

    def prompt_scaffold(self) -> dict[str, Any]:
        profile = dict(self.pack.resolved_profile or {})
        prompt_view = _prompt_view_from_pack(self.pack)
        unit_scope = dict(prompt_view.get("unit") or {}) if prompt_view else {}
        requirements_brief = dict((self.pack.metadata or {}).get("requirements_brief") or {})
        brief_acceptance = [
            str(item).strip()
            for item in list(requirements_brief.get("acceptance_criteria") or [])
            if str(item or "").strip()
        ]
        acceptance = brief_acceptance or list(self.pack.acceptance_criteria)
        instruction = str(self.pack.instruction or self.pack.goal)
        return {
            "identity": str(profile.get("identity_fragment") or ""),
            "behavior": str(profile.get("behavior_fragment") or ""),
            "instruction": instruction,
            "acceptance_criteria": acceptance,
            "continuity": dict(self.pack.continuity),
            "unit_scope": unit_scope,
            "allowed_capabilities": list(self.pack.allowed_capabilities),
            "visible_capabilities": list(self.tool_session.visible_capability_aliases),
            "skill_manual_context": list((self.pack.metadata or {}).get("skill_manual_context") or []),
            "initial_skill_injections": list(
                (self.pack.metadata or {}).get("initial_skill_injections") or []
            ),
            "output_contract": str(profile.get("output_contract_fragment") or ""),
            "workspace_policy": self.workspace_policy(),
            "completion_policy": self.completion_policy(),
            "execution_strategy": self.execution_strategy(),
            "prompt_view": prompt_view,
            "requirements_brief": requirements_brief,
            "workflow_model": "contract_v2",
        }

    def workspace_policy(self) -> dict[str, Any]:
        workspace_policy = self.pack.workspace.get("workspace_policy")
        if isinstance(workspace_policy, dict):
            return dict(workspace_policy)
        profile = dict(self.pack.resolved_profile or {})
        if isinstance(profile.get("effective_workspace_policy"), dict):
            return dict(profile.get("effective_workspace_policy") or {})
        return {}

    def completion_policy(self) -> dict[str, Any]:
        completion_policy = self.pack.workspace.get("completion_policy")
        if isinstance(completion_policy, dict):
            return dict(completion_policy)
        profile = dict(self.pack.resolved_profile or {})
        if isinstance(profile.get("effective_completion_policy"), dict):
            return dict(profile.get("effective_completion_policy") or {})
        return {}

    def execution_strategy(self) -> dict[str, Any]:
        return {}

    def policy_from_workspace_or_profile(self, key: str) -> dict[str, Any]:
        value = self.pack.workspace.get(key)
        if isinstance(value, dict):
            return dict(value)
        profile = dict(self.pack.resolved_profile or {})
        effective_key = f"effective_{key}"
        if isinstance(profile.get(effective_key), dict):
            return dict(profile.get(effective_key) or {})
        if isinstance(profile.get(key), dict):
            return dict(profile.get(key) or {})
        return {}
