from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pal.execution.capability_registry import CapabilityRegistry
from pal.execution.capability_compiler import compile_provider_subtree
from pal.execution.contracts import (
    CapabilityCall,
    CapabilityDescriptor,
    CapabilityCallable,
    CapabilityResult,
    ExecutionRuntimePort,
    RegisteredCapability,
    Tool,
)
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.plugins.l3.registry import L3PluginRegistry
from pal.plugins.l3.stubs import NullL3Plugin
from pal.shared import (
    BoundActionIndex,
    BoundCapabilityAction,
    CapabilityForestRegistry,
    CompiledCapabilityIndex,
    RuntimeStatus,
    SINGLETON_TARGET,
)

if TYPE_CHECKING:
    from pal.core.module_registry import ModuleHandle


@dataclass
class ExecutionRuntime(ExecutionRuntimePort):
    capability_registry: CapabilityRegistry = field(default_factory=CapabilityRegistry)
    capabilities: dict[str, RegisteredCapability] = field(default_factory=dict)
    capability_forest: CapabilityForestRegistry = field(default_factory=CapabilityForestRegistry)
    compiled_capability_index: CompiledCapabilityIndex = field(default_factory=CompiledCapabilityIndex)
    bound_action_index: BoundActionIndex = field(default_factory=BoundActionIndex)
    tools: dict[str, Tool] = field(default_factory=dict)
    provider_registry: dict[str, Any] = field(default_factory=dict)
    l3_plugin_registry: L3PluginRegistry = field(default_factory=L3PluginRegistry)

    def __post_init__(self) -> None:
        default_l3 = NullL3Plugin()
        self.provider_registry.setdefault(default_l3.provider_id, default_l3)
        if self.l3_plugin_registry.get(default_l3.provider_id) is None:
            self.l3_plugin_registry.register(default_l3)

    def register_capability(self, descriptor: CapabilityDescriptor, callable: CapabilityCallable) -> None:
        self.capabilities[descriptor.name] = RegisteredCapability(descriptor=descriptor, callable=callable)
        self.bound_action_index.register(
            BoundCapabilityAction(
                canonical_path=descriptor.canonical_path or descriptor.name,
                target_id=descriptor.target_id or SINGLETON_TARGET,
                descriptor=descriptor,
                callable=callable,
            )
        )

    def unregister_capability(self, name: str) -> None:
        registered = self.capabilities.pop(name, None)
        if registered is not None:
            self.bound_action_index.unregister_many(
                [
                    (
                        registered.descriptor.canonical_path or registered.descriptor.name,
                        registered.descriptor.target_id or SINGLETON_TARGET,
                    )
                ]
            )

    def register_provider_ref(self, provider_id: str, provider: Any) -> None:
        self.provider_registry[provider_id] = provider

    def unregister_provider_ref(self, provider_id: str) -> None:
        self.provider_registry.pop(provider_id, None)

    def register_tool(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def list_tool_specs(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for name in sorted(self.tools):
            tool = self.tools[name]
            specs.append(
                {
                    "name": tool.name,
                    "display_name": str(getattr(tool, "display_name", "") or tool.name),
                    "family": str(getattr(tool, "family", "") or "general"),
                    "description": str(getattr(tool, "description", "") or f"Tool {tool.name}"),
                    "tags": list(getattr(tool, "tags", ()) or ()),
                    "keywords": list(getattr(tool, "keywords", ()) or ()),
                    "args_schema": dict(getattr(tool, "args_schema", {}) or {"type": "object", "properties": {}}),
                    "result_schema": dict(getattr(tool, "result_schema", {}) or {"type": "object", "properties": {}}),
                }
            )
        return specs

    def get_tool_spec(self, name: str) -> dict[str, Any] | None:
        tool = self.tools.get(name)
        if tool is None:
            return None
        return {
            "name": tool.name,
            "display_name": str(getattr(tool, "display_name", "") or tool.name),
            "family": str(getattr(tool, "family", "") or "general"),
            "description": str(getattr(tool, "description", "") or f"Tool {tool.name}"),
            "tags": list(getattr(tool, "tags", ()) or ()),
            "keywords": list(getattr(tool, "keywords", ()) or ()),
            "args_schema": dict(getattr(tool, "args_schema", {}) or {"type": "object", "properties": {}}),
            "result_schema": dict(getattr(tool, "result_schema", {}) or {"type": "object", "properties": {}}),
        }

    def list_capability_specs(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for name in sorted(self.compiled_capability_index.records):
            descriptor = self.compiled_capability_index.records[name]
            specs.append(
                {
                    "name": descriptor.canonical_path or descriptor.name,
                    "display_name": descriptor.display_name or descriptor.canonical_path or descriptor.name,
                    "family": descriptor.family,
                    "description": descriptor.description,
                    "module_id": descriptor.module_id,
                    "aliases": list(descriptor.aliases),
                    "parameters_schema": dict(descriptor.parameters_schema or {"type": "object", "properties": {}}),
                    "result_schema": dict(descriptor.result_schema or {"type": "object", "properties": {}}),
                    "source": descriptor.source,
                    "target_kind": descriptor.target_kind,
                    "target_id": descriptor.target_id,
                }
            )
        return specs

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        if name in self.compiled_capability_index.by_canonical:
            record_ids = self.compiled_capability_index.by_canonical[name]
            if record_ids:
                descriptor = self.compiled_capability_index.records[record_ids[0]]
                return {
                    "canonical_path": descriptor.canonical_path or descriptor.name,
                    "name": descriptor.canonical_path or descriptor.name,
                    "display_name": descriptor.display_name or descriptor.canonical_path or descriptor.name,
                    "family": descriptor.family,
                    "description": descriptor.description,
                    "module_id": descriptor.module_id,
                    "aliases": list(descriptor.aliases),
                    "parameters_schema": dict(descriptor.parameters_schema or {"type": "object", "properties": {}}),
                    "result_schema": dict(descriptor.result_schema or {"type": "object", "properties": {}}),
                    "source": descriptor.source,
                    "target_kind": descriptor.target_kind,
                    "target_id": descriptor.target_id,
                }
        alias_matches = self.compiled_capability_index.aliases.get(name, [])
        if alias_matches:
            descriptor = self.compiled_capability_index.records[alias_matches[0]]
            return {
                "canonical_path": descriptor.canonical_path or descriptor.name,
                "name": descriptor.canonical_path or descriptor.name,
                "display_name": descriptor.display_name or descriptor.canonical_path or descriptor.name,
                "family": descriptor.family,
                "description": descriptor.description,
                "module_id": descriptor.module_id,
                "aliases": list(descriptor.aliases),
                "parameters_schema": dict(descriptor.parameters_schema or {"type": "object", "properties": {}}),
                "result_schema": dict(descriptor.result_schema or {"type": "object", "properties": {}}),
                "source": descriptor.source,
                "target_kind": descriptor.target_kind,
                "target_id": descriptor.target_id,
            }
        registered = self.capabilities.get(name)
        if registered is None:
            return None
        descriptor = registered.descriptor
        return {
            "canonical_path": descriptor.canonical_path or descriptor.name,
            "name": descriptor.canonical_path or descriptor.name,
            "display_name": descriptor.display_name or descriptor.canonical_path or descriptor.name,
            "family": descriptor.family,
            "description": descriptor.description,
            "module_id": descriptor.module_id,
            "aliases": list(descriptor.aliases),
            "parameters_schema": dict(descriptor.parameters_schema or {"type": "object", "properties": {}}),
            "result_schema": dict(descriptor.result_schema or {"type": "object", "properties": {}}),
            "source": descriptor.source,
            "target_kind": descriptor.target_kind,
            "target_id": descriptor.target_id,
        }

    def hydrate_module_handle(self, handle: "ModuleHandle") -> None:
        provider = handle.introspection_provider
        if provider is None:
            return
        # Hydration is separate from publication. At this point we only compile
        # the subtree from blueprint metadata; PalCore decides when it becomes
        # visible and callable.
        handle.mounted_subtree = compile_provider_subtree(
            provider,
            module_id=handle.module_id,
            lifecycle_scope=handle.tier,
            detachable=handle.detachable,
        )

    def mount_subtree(self, handle: "ModuleHandle") -> list[str]:
        subtree = handle.mounted_subtree
        if subtree is None:
            return []
        if subtree.mounted:
            return [descriptor.name for descriptor in subtree.descriptors]
        # Mount updates all three runtime views together:
        # 1. forest nodes
        # 2. fuzzy search records
        # 3. exact O(1) dispatch entries
        self.capability_forest.mount(subtree)
        for descriptor in subtree.descriptors:
            self.compiled_capability_index.register(descriptor)
            self.capability_registry.register(descriptor)
        for bound_action in subtree.bound_actions:
            self.bound_action_index.register(bound_action)
            self.capabilities[bound_action.descriptor.name] = RegisteredCapability(
                descriptor=bound_action.descriptor,
                callable=bound_action.callable,
            )
        return [descriptor.name for descriptor in subtree.descriptors]

    def unmount_subtree(self, handle: "ModuleHandle") -> list[str]:
        subtree = handle.mounted_subtree
        if subtree is None or not subtree.mounted:
            return []
        # Teardown must be exact. The mounted subtree handle records every key
        # and record id so we never rely on prefix scans or whole-table walks.
        self.bound_action_index.unregister_many(subtree.bound_action_keys)
        self.compiled_capability_index.unregister_many(subtree.search_record_ids)
        for descriptor in subtree.descriptors:
            self.capabilities.pop(descriptor.name, None)
            self.capability_registry.unregister(descriptor.name)
        self.capability_forest.unmount(subtree)
        return list(subtree.search_record_ids)

    def execute_tool(self, call: CanonicalToolCall, *, allow_tools: bool = True) -> CanonicalToolResult:
        call_id = getattr(call, "call_id", None)
        try:
            if not allow_tools:
                return CanonicalToolResult(
                    name=call.name,
                    ok=False,
                    text="tool execution disabled in finalization mode",
                    structured={"reason": "finalization_only"},
                    call_id=call_id,
                    llm_text="tool execution disabled in finalization mode",
                )
            tool = self.tools.get(call.name)
            if tool is not None:
                result = tool.invoke(call.args)
                return CanonicalToolResult(
                    name=call.name,
                    ok=result.status == RuntimeStatus.OK,
                    text=result.text,
                    structured=result.structured,
                    call_id=call_id,
                    llm_text=getattr(result, "llm_text", ""),
                )
            capability_result = self.execute(CapabilityCall(name=call.name, args=dict(call.args)))
            if capability_result.status == RuntimeStatus.ERROR and str(capability_result.text).startswith("unknown capability:"):
                return CanonicalToolResult(
                    name=call.name,
                    ok=False,
                    text=f"unknown tool: {call.name}",
                    structured={"reason": "unknown_tool"},
                    call_id=call_id,
                    llm_text=f"unknown tool: {call.name}",
                )
            return CanonicalToolResult(
                name=call.name,
                ok=capability_result.status == RuntimeStatus.OK,
                text=capability_result.text,
                structured=capability_result.structured,
                call_id=call_id,
                llm_text=getattr(capability_result, "llm_text", ""),
            )
        except Exception as exc:
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=f"tool execution failed: {exc.__class__.__name__}",
                structured={"error": str(exc), "tool": call.name},
                call_id=call_id,
                llm_text=f"tool execution failed: {exc.__class__.__name__}",
            )

    async def execute_tool_async(self, call: CanonicalToolCall, *, allow_tools: bool = True) -> CanonicalToolResult:
        return await asyncio.to_thread(self.execute_tool, call, allow_tools=allow_tools)

    def call_registered(self, call: CapabilityCall) -> CapabilityResult:
        target_id = str(call.args.get("target_id") or SINGLETON_TARGET)
        bound = self.bound_action_index.get(call.name, target_id)
        if bound is not None:
            return bound.callable(call)
        matching = self.compiled_capability_index.by_canonical.get(call.name, [])
        if matching and target_id == SINGLETON_TARGET:
            # Search may be fuzzy, but execution must be strict. If the caller
            # asks for an instance-level canonical path without a target_id,
            # fail with the available targets instead of guessing.
            descriptors = [self.compiled_capability_index.records[record_id] for record_id in matching]
            instance_targets = sorted(
                {
                    descriptor.target_id
                    for descriptor in descriptors
                    if descriptor.target_id and descriptor.target_id != SINGLETON_TARGET
                }
            )
            if instance_targets:
                return CapabilityResult(
                    status=RuntimeStatus.INVALID,
                    text="target_id is required for this capability",
                    structured={"canonical_path": call.name, "available_target_ids": instance_targets},
                    llm_text="target_id is required for this capability",
                )
        registered = self.capabilities.get(call.name)
        if registered is None:
            alias_matches = self.compiled_capability_index.aliases.get(call.name, [])
            if alias_matches:
                if target_id == SINGLETON_TARGET:
                    descriptors = [self.compiled_capability_index.records[record_id] for record_id in alias_matches]
                    instance_targets = sorted(
                        {
                            descriptor.target_id
                            for descriptor in descriptors
                            if descriptor.target_id and descriptor.target_id != SINGLETON_TARGET
                        }
                    )
                    if instance_targets:
                        return CapabilityResult(
                            status=RuntimeStatus.INVALID,
                            text="target_id is required for this capability",
                            structured={"canonical_path": call.name, "available_target_ids": instance_targets},
                            llm_text="target_id is required for this capability",
                        )
                registered = self.capabilities.get(alias_matches[0])
        if registered is None:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text=f"unknown capability: {call.name}",
                llm_text=f"unknown capability: {call.name}",
            )
        return registered.callable(call)

    def execute(self, call: CapabilityCall) -> CapabilityResult:
        try:
            result = self.call_registered(call)
            return CapabilityResult(
                status=result.status,
                text=result.text,
                structured=result.structured,
                llm_text=getattr(result, "llm_text", ""),
            )
        except Exception as exc:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text=f"capability execution failed: {exc.__class__.__name__}",
                structured={"error": str(exc), "capability": call.name},
                llm_text=f"capability execution failed: {exc.__class__.__name__}",
            )

    async def execute_async(self, call: CapabilityCall) -> CapabilityResult:
        return await asyncio.to_thread(self.execute, call)
