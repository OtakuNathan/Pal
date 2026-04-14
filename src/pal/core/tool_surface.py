from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pal.shared import SINGLETON_TARGET

_CONFIG_PATH = Path(__file__).parent / "tool_surface.toml"


class ToolSurface:
    def __init__(self, context) -> None:
        self.context = context
        self._config = self._load_config()

    @staticmethod
    def _load_config() -> dict[str, Any]:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f)

    def build_llm_tool_contracts(self) -> list[dict[str, object]]:
        return self.build_tool_contracts_from_descriptors(self.select_llm_descriptors())

    def build_tool_contracts_from_descriptors(self, descriptors: list[Any]) -> list[dict[str, object]]:
        contracts: list[dict[str, object]] = []
        for descriptor in sorted(descriptors, key=lambda item: item.name):
            contracts.append(
                {
                    "type": "function",
                    "function": {
                        "name": descriptor.canonical_path or descriptor.name,
                        "description": str(descriptor.description or descriptor.name),
                        "parameters": dict(descriptor.parameters_schema or {"type": "object", "properties": {}}),
                    },
                }
            )
        return contracts

    def select_llm_descriptors(self) -> list[Any]:
        execution_runtime = self.context.execution_runtime
        records = execution_runtime.compiled_capability_index.records
        by_canonical = execution_runtime.compiled_capability_index.by_canonical
        llm_descriptors: list[Any] = []
        seen: set[tuple[str, str]] = set()

        # -- singleton capabilities from config --
        for canonical_path in self._config.get("singletons", {}).get("capabilities", []):
            for record_id in by_canonical.get(canonical_path, []):
                descriptor = records[record_id]
                if descriptor.target_id != SINGLETON_TARGET:
                    continue
                key = (descriptor.canonical_path or descriptor.name, descriptor.target_id or SINGLETON_TARGET)
                if key in seen:
                    continue
                seen.add(key)
                llm_descriptors.append(descriptor)

        # -- dynamic capabilities from config --
        for entry in self._config.get("dynamic", []):
            canonical_path = entry.get("canonical_path", "")
            provider_setting = entry.get("provider_setting", "")
            if not canonical_path or not provider_setting:
                continue
            active_target_id = self._resolve_dynamic_target(provider_setting)
            if not active_target_id:
                continue
            for record_id in by_canonical.get(canonical_path, []):
                descriptor = records[record_id]
                if descriptor.target_id != active_target_id:
                    continue
                key = (descriptor.canonical_path or descriptor.name, descriptor.target_id or SINGLETON_TARGET)
                if key in seen:
                    continue
                seen.add(key)
                llm_descriptors.append(descriptor)
                break

        return llm_descriptors

    def _resolve_dynamic_target(self, provider_setting: str) -> str:
        if provider_setting == "memory":
            return self._get_active_l3_provider_id()
        return self._get_setting(provider_setting)

    def _get_active_l3_provider_id(self) -> str:
        try:
            memory_service = self.context.require_port("memory:memory")
        except KeyError:
            return ""
        return str(memory_service.l3_selector.active_provider_id or "").strip()

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
                if target_id is not None and descriptor_target != target_id:
                    continue
                key = (descriptor.canonical_path or descriptor.name, descriptor_target)
                if key in seen:
                    continue
                seen.add(key)
                selected.append(descriptor)

        include("operation_execution_discovery_read")

        if signal.subsystem == "memory":
            include("introspection_module_memory_show")
            include("introspection_module_memory_list_providers")
            include("introspection_module_memory_active_provider")
            include("operation_memory_management_set_active_provider")
            try:
                memory_service = self.context.require_port("memory:memory")
                active_provider_id = str(memory_service.l3_selector.active_provider_id or "").strip()
            except KeyError:
                active_provider_id = ""
            if active_provider_id:
                include("introspection_provider_l3_show", target_id=active_provider_id)
                include("introspection_provider_l3_inventory", target_id=active_provider_id)
                include("operation_l3_maintenance_refresh_indexes", target_id=active_provider_id)
        elif signal.subsystem in {"plugin", "plugins"}:
            include("introspection_module_plugins_show")
            include("introspection_module_plugins_list")
            include("operation_plugin_management_rescan")
            include("operation_plugin_management_enable")
            include("operation_plugin_management_disable")
        elif signal.subsystem == "channel":
            include("introspection_module_channel_list")
            include("operation_channel_management_enable")
            include("operation_channel_management_disable")
            include("operation_channel_management_attach")
            include("operation_channel_management_detach")
            endpoint_id = (
                str(signal.related_ids.get("endpoint_id") or "").strip()
                or str(signal.component or "").strip()
            )
            if endpoint_id:
                include("introspection_endpoint_channel_inspect", target_id=endpoint_id)
                include("introspection_endpoint_channel_auth_state", target_id=endpoint_id)
                include("introspection_endpoint_channel_backlog", target_id=endpoint_id)
                include("introspection_endpoint_channel_health", target_id=endpoint_id)
        elif signal.subsystem == "llm":
            include("introspection_module_llm_list")
            include("introspection_module_llm_active")
            include("introspection_module_llm_think_level")
        elif signal.subsystem == "execution":
            include("introspection_module_execution_show")
            include("introspection_module_execution_tools")
        elif signal.subsystem == "web_search":
            include("introspection_module_web_search_show")
            include("introspection_module_web_search_active_provider")
            include("introspection_module_web_search_list_providers")
            active_web_search_provider_id = self._get_setting("active_web_search_provider_id")
            if active_web_search_provider_id:
                include("introspection_provider_web_search_health", target_id=active_web_search_provider_id)
                include("operation_web_search_management_set_active_provider")
        elif signal.subsystem == "web_fetch":
            include("introspection_module_web_fetch_show")
            include("introspection_module_web_fetch_active_provider")
            include("introspection_module_web_fetch_list_providers")
            active_web_fetch_provider_id = self._get_setting("active_web_fetch_provider_id")
            if active_web_fetch_provider_id:
                include("introspection_provider_web_fetch_health", target_id=active_web_fetch_provider_id)
                include("operation_web_fetch_management_set_active_provider")
        else:
            module_name = str(signal.subsystem or "").strip()
            if module_name:
                include(f"introspection_module_{module_name}_show")
        return selected
