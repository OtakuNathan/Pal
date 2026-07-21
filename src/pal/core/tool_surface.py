from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pal.shared import (
    SINGLETON_TARGET,
)

_CONFIG_PATH = Path(__file__).parent / "tool_surface.toml"


class ToolSurface:
    def __init__(self, context) -> None:
        self.context = context
        self._config = self._load_config()

    @staticmethod
    def _load_config() -> dict[str, Any]:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f)

    def reload_config(self) -> dict[str, Any]:
        self._config = self._load_config()
        singletons = [
            str(item).strip()
            for item in list(self._config.get("singletons", {}).get("capabilities", []) or [])
            if str(item).strip()
        ]
        dynamic = [
            str(item.get("canonical_path") or "").strip()
            for item in list(self._config.get("dynamic", []) or [])
            if isinstance(item, dict) and str(item.get("canonical_path") or "").strip()
        ]
        resident = [
            str(contract["function"]["name"])
            for contract in self.build_llm_tool_contracts()
            if isinstance(contract.get("function"), dict) and contract["function"].get("name")
        ]
        return {
            "config_path": str(_CONFIG_PATH),
            "singleton_count": len(singletons),
            "dynamic_count": len(dynamic),
            "resident_tool_count": len(resident),
            "resident_tool_names": resident,
        }

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
                    if item.descriptor_name == descriptor.name
                ),
                None,
            )
            if record is None:
                continue
            contract = generation.provider_specs.get(record.alias)
            if contract is not None:
                contracts.append(dict(contract))
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
            include("op_channel_mgmt_disable")
            include("op_channel_mgmt_attach")
            include("op_channel_mgmt_detach")
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
