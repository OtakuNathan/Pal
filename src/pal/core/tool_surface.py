from __future__ import annotations

from typing import Any

from pal.shared import (
    SINGLETON_TARGET,
)


class ToolSurface:
    """Project registry generations into LLM tool contracts and failure surfaces.

    DIRECT descriptors and explicit owner ``native_function`` opt-ins expose
    full schemas. Opt-ins retain their declared invocation/effect semantics;
    model execution routes them through the same validated runtime path.
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
        generation = execution_runtime.registry_generation
        records = generation.capability_index.records
        by_canonical = generation.capability_index.by_canonical
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

        def include_alias(alias: str, *, target_id: str | None = None) -> None:
            record = generation.record_for_alias(alias)
            if record is not None:
                include(record.canonical_path, target_id=target_id)

        include_alias("read_tool")

        if signal.subsystem == "memory":
            include_alias("inspect_memory_state")
        elif signal.subsystem in {"plugin", "plugins"}:
            include_alias("inspect_plugin")
            include_alias("list_plugins")
            include_alias("rescan_plugins")
            include_alias("manage_plugin")
        elif signal.subsystem == "channel":
            include_alias("inspect_channel_state")
            include_alias("list_channel_endpoints")
            include_alias("manage_channel_endpoint")
            endpoint_id = (
                str(signal.related_ids.get("endpoint_id") or "").strip()
                or str(signal.component or "").strip()
            )
            if endpoint_id:
                include_alias("inspect_channel_endpoint", target_id=endpoint_id)
        elif signal.subsystem == "llm":
            include_alias("inspect_llm")
        elif signal.subsystem == "execution":
            include_alias("inspect_execution_state")
            include_alias("search_tools")
        elif signal.subsystem == "web_search":
            include_alias("inspect_web_search")
        elif signal.subsystem == "web_fetch":
            include_alias("inspect_browser_status")
        else:
            module_name = str(signal.subsystem or "").strip()
            if module_name:
                include(f"intro_module_{module_name}_show")
        return selected
