from __future__ import annotations

from typing import Any

from pal.shared import (
    SINGLETON_TARGET,
)


class ToolSurface:
    """Project registry generations into LLM tool contracts and failure surfaces.

    Direct LLM tool exposure is fully determined by capability descriptor
    ``invocation_mode`` at registry compile time: DIRECT descriptors are
    compiled into ``provider_specs`` and exposed as function-calling tools,
    INDIRECT descriptors stay discoverable via tool search. There is no
    config file and no runtime refresh path for the normal-turn surface.
    """

    def __init__(self, context) -> None:
        self.context = context

    def build_llm_tool_contracts(self) -> list[dict[str, object]]:
        generation = self.context.execution_runtime.registry_generation
        return [dict(generation.provider_specs[alias]) for alias in sorted(generation.provider_specs)]

    def build_tool_contracts_from_descriptors(self, descriptors: list[Any]) -> list[dict[str, object]]:
        contracts: list[dict[str, object]] = []
        generation = self.context.execution_runtime.registry_generation
        for descriptor in sorted(descriptors, key=lambda item: item.name):
            record = next(
                (
                    item
                    for item in generation.direct_aliases.values()
                    if item.alias in descriptor.aliases
                ),
                None,
            )
            if record is None:
                continue
            contract = generation.provider_specs.get(record.alias)
            if contract is not None:
                contracts.append(dict(contract))
        return contracts

    def _get_setting(self, key: str) -> str:
        try:
            from pal.llm.repository import RuntimeSettingRepository
            return str(RuntimeSettingRepository().get(key) or "").strip()
        except Exception:
            return ""

    def select_failure_descriptors(self, signal) -> list[Any]:
        execution_runtime = self.context.execution_runtime
        records = execution_runtime.compiled_capability_index.records
        by_canonical = execution_runtime.compiled_capability_index.by_canonical
        selected: list[Any] = []
        seen: set[tuple[str, str]] = set()

        def include(canonical_path: str, *, target_id: str | None = None) -> None:
            for record_id in by_canonical.get(canonical_path, []):
                descriptor = records[record_id]
                descriptor_target = descriptor.target_id or SINGLETON_TARGET
                is_parameterized = bool(descriptor.metadata.get("target_argument"))
                if target_id is not None and not is_parameterized and descriptor_target != target_id:
                    continue
                key = (
                    descriptor.canonical_path or descriptor.name,
                    SINGLETON_TARGET if is_parameterized else descriptor_target,
                )
                if key in seen:
                    continue
                seen.add(key)
                selected.append(descriptor)

        include("op_tool_read")

        if signal.subsystem == "memory":
            include("intro_module_memory_show")
            include("intro_module_memory_list_providers")
            include("intro_module_memory_active_provider")
            include("op_memory_mgmt_set_active_provider")
            try:
                memory_service = self.context.require_port("memory:memory")
                active_provider_id = str(memory_service.l3_selector.active_provider_id or "").strip()
            except KeyError:
                active_provider_id = ""
            if active_provider_id:
                include("intro_provider_memory_show", target_id=active_provider_id)
                include("intro_provider_memory_inventory", target_id=active_provider_id)
        elif signal.subsystem in {"plugin", "plugins"}:
            include("intro_module_plugins_show")
            include("intro_module_plugins_list")
            include("op_plugin_mgmt_rescan")
            include("op_plugin_mgmt_enable")
            include("op_plugin_mgmt_disable")
        elif signal.subsystem == "channel":
            include("intro_module_channel_list")
            include("op_channel_mgmt_enable")
            include("op_channel_mgmt_attach")
            include("op_channel_mgmt_restart_endpoint")
            endpoint_id = (
                str(signal.related_ids.get("endpoint_id") or "").strip()
                or str(signal.component or "").strip()
            )
            if endpoint_id:
                include("intro_endpoint_channel_inspect", target_id=endpoint_id)
                include("intro_endpoint_channel_auth_state", target_id=endpoint_id)
                include("intro_endpoint_channel_backlog", target_id=endpoint_id)
                include("intro_endpoint_channel_health", target_id=endpoint_id)
        elif signal.subsystem == "llm":
            include("intro_module_llm_list")
            include("intro_module_llm_active")
            include("intro_module_llm_think_level")
        elif signal.subsystem == "execution":
            include("intro_module_exec_show")
            include("intro_module_exec_tools")
        elif signal.subsystem == "web_search":
            include("intro_module_web_search_show")
            include("intro_module_web_search_active_provider")
            include("intro_module_web_search_list_providers")
            active_web_search_provider_id = self._get_setting("active_web_search_provider_id")
            if active_web_search_provider_id:
                include("intro_provider_web_search_health", target_id=active_web_search_provider_id)
                include("op_web_search_mgmt_set_active_provider")
        elif signal.subsystem == "web_fetch":
            include("intro_module_web_fetch_show")
            include("intro_module_web_fetch_active_provider")
            include("intro_module_web_fetch_list_providers")
            active_web_fetch_provider_id = self._get_setting("active_web_fetch_provider_id")
            if active_web_fetch_provider_id:
                include("intro_provider_web_fetch_health", target_id=active_web_fetch_provider_id)
                include("op_web_fetch_mgmt_set_active_provider")
        else:
            module_name = str(signal.subsystem or "").strip()
            if module_name:
                include(f"intro_module_{module_name}_show")
        return selected
