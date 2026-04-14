from __future__ import annotations

from dataclasses import dataclass

from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider


@dataclass
class MinimalOperatingRulesPromptFragmentProvider(PromptFragmentProvider):
    provider_id: str = "core.prompt.minimal_rules"
    module_id: str = "core"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        return [
            PromptFragment(
                section="rules",
                title="Minimal Operating Rules",
                content=(
                    "Directly answer the user when you already have enough information.\n"
                    "Use capabilities only when they are actually needed to answer, inspect, or act.\n"
                    "If you need to discover available capabilities or confirm how to call one, use "
                    "`operation_execution_discovery_search` and `operation_execution_discovery_read`.\n"
                    "Do not call discovery by default when a direct answer is enough.\n"
                    "You operate through capabilities, not raw tools."
                ),
                priority=90,
            ),
            PromptFragment(
                section="capability_guide",
                title="Capability Guide",
                content=(
                    "`introspection_*` capabilities are for internal observation: inspect modules, state, configuration, "
                    "history, health, and capability definitions. They should not change system or external state.\n"
                    "`operation_*` capabilities are for external action: execute commands, perform mutations, manage "
                    "modules, submit changes, or affect outside systems.\n"
                    "For memory work, use the explicit memory capabilities:\n"
                    "- `operation_l3_recall_query` to recall durable memory.\n"
                    "- `operation_l3_commit_write` to commit durable memory.\n"
                    "- `operation_l3_correct_patch` to correct durable memory.\n"
                    "- `operation_l3_maintenance_refresh_indexes` to refresh pending or stale memory indexes when needed.\n"
                    "- `introspection_module_memory_active_provider` and `operation_memory_management_set_active_provider` to inspect or switch the active memory provider.\n"
                    "- `introspection_provider_l3_show` and `introspection_provider_l3_inventory` to inspect the active L3 provider.\n"
                    "For web access, use the explicit web capabilities:\n"
                    "- `operation_web_search_query` to search the web.\n"
                    "- `operation_web_fetch_read` to open a webpage and read its content.\n"
                    "- `introspection_module_web_search_active_provider` and `introspection_module_web_fetch_active_provider` to inspect configured web providers.\n"
                    "- `introspection_provider_web_search_health` and `introspection_provider_web_fetch_health` to inspect provider health when web calls fail.\n"
                    "If you need to discover or confirm a capability definition, use `operation_execution_discovery_search` and `operation_execution_discovery_read`.\n"
                    "Use the smallest capability that answers the need; if existing information is enough, answer directly."
                ),
                priority=91,
            ),
        ]
