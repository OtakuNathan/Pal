from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pal.foundation.artifact import ArtifactIngestor, StoredArtifact
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
    ToolCallBudget,
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
    runtime_root: Path | None = None
    lifecycle_controller: Any | None = None
    sync_executor_max_workers: int = 4
    sync_executor: ThreadPoolExecutor | None = None
    _interrupt_handles: dict[str, set[Any]] = field(default_factory=dict)
    _interrupt_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _interrupt_state_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        default_l3 = NullL3Plugin()
        self.provider_registry.setdefault(default_l3.provider_id, default_l3)
        if self.l3_plugin_registry.get(default_l3.provider_id) is None:
            self.l3_plugin_registry.register(default_l3)
        if self.sync_executor is None:
            self.sync_executor = ThreadPoolExecutor(
                max_workers=self.sync_executor_max_workers,
                thread_name_prefix="pal-exec",
            )

    def shutdown(self) -> None:
        if self.sync_executor is not None:
            self.sync_executor.shutdown(wait=False, cancel_futures=True)
            self.sync_executor = None

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
            specs.append(_capability_spec_payload(descriptor))
        return specs

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        descriptor = self._resolve_descriptor(name)
        if isinstance(descriptor, CapabilityResult):
            descriptor = self._first_descriptor_match(name)
        if descriptor is not None:
            return _capability_spec_payload(descriptor)
        registered = self.capabilities.get(name)
        if registered is None:
            return None
        return _capability_spec_payload(registered.descriptor)

    def _first_descriptor_match(self, name: str) -> CapabilityDescriptor | None:
        for index in (self.compiled_capability_index.by_canonical, self.compiled_capability_index.aliases):
            record_ids = index.get(name, [])
            for record_id in record_ids:
                descriptor = self.compiled_capability_index.records.get(record_id)
                if descriptor is not None:
                    return descriptor
        return None

    def hydrate_module_handle(self, handle: "ModuleHandle") -> None:
        provider = handle.introspection_provider
        if provider is None:
            return
        handle.mounted_subtree = compile_provider_subtree(
            provider,
            module_id=handle.module_id,
            lifecycle_scope=handle.tier,
            detachable=handle.detachable,
        )
        build_dynamic_subtree = getattr(provider, "build_mounted_subtree", None)
        if callable(build_dynamic_subtree):
            dynamic = build_dynamic_subtree(
                module_id=handle.module_id,
                lifecycle_scope=handle.tier,
                detachable=handle.detachable,
            )
            if dynamic is not None:
                handle.mounted_subtree.nodes.extend(dynamic.nodes)
                handle.mounted_subtree.descriptors.extend(dynamic.descriptors)
                handle.mounted_subtree.bound_actions.extend(dynamic.bound_actions)
                handle.mounted_subtree.node_ids.extend(dynamic.node_ids)
                handle.mounted_subtree.bound_action_keys.extend(dynamic.bound_action_keys)
                handle.mounted_subtree.search_record_ids.extend(dynamic.search_record_ids)
            return

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

    def execute_tool(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
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
                    status="finalization_only",
                )
            tool = self.tools.get(call.name)
            if tool is not None:
                result = tool.invoke(call.args)
                canonical_result = CanonicalToolResult(
                    name=call.name,
                    ok=result.status == RuntimeStatus.OK,
                    text=result.text,
                    structured=result.structured,
                    call_id=call_id,
                    llm_text=getattr(result, "llm_text", ""),
                    status=result.status,
                )
                return self._apply_tool_budget(call, canonical_result, budget=budget)
            meta = {"turn_id": str(turn_id)} if str(turn_id or "").strip() else {}
            capability_result = self.execute(CapabilityCall(name=call.name, args=dict(call.args), meta=meta))
            if capability_result.status == RuntimeStatus.ERROR and str(capability_result.text).startswith("unknown capability:"):
                return CanonicalToolResult(
                    name=call.name,
                    ok=False,
                    text=f"unknown tool: {call.name}",
                    structured={"reason": "unknown_tool"},
                    call_id=call_id,
                    llm_text=f"unknown tool: {call.name}",
                    status="unknown_tool",
                )
            canonical_result = CanonicalToolResult(
                name=call.name,
                ok=capability_result.status == RuntimeStatus.OK,
                text=capability_result.text,
                structured=capability_result.structured,
                call_id=call_id,
                llm_text=getattr(capability_result, "llm_text", ""),
                status=capability_result.status,
            )
            return self._apply_tool_budget(call, canonical_result, budget=budget)
        except Exception as exc:
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=f"tool execution failed: {exc.__class__.__name__}",
                structured={"error": str(exc), "tool": call.name},
                call_id=call_id,
                llm_text=f"tool execution failed: {exc.__class__.__name__}",
                status=RuntimeStatus.ERROR,
            )

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        if not allow_tools:
            return self.execute_tool(call, allow_tools=allow_tools, budget=budget, turn_id=turn_id)
        tool = self.tools.get(call.name)
        if tool is not None:
            async_invoke = getattr(tool, "ainvoke", None)
            if callable(async_invoke):
                try:
                    result = await async_invoke(dict(call.args), runtime=self, turn_id=turn_id)
                    canonical = CanonicalToolResult(
                        name=call.name,
                        ok=result.status == RuntimeStatus.OK,
                        text=result.text,
                        structured=result.structured,
                        call_id=getattr(call, "call_id", None),
                        llm_text=getattr(result, "llm_text", ""),
                        status=result.status,
                    )
                    return self._apply_tool_budget(call, canonical, budget=budget)
                except Exception as exc:
                    return CanonicalToolResult(
                        name=call.name,
                        ok=False,
                        text=f"tool execution failed: {exc.__class__.__name__}",
                        structured={"error": str(exc), "tool": call.name},
                        call_id=getattr(call, "call_id", None),
                        llm_text=f"tool execution failed: {exc.__class__.__name__}",
                        status=RuntimeStatus.ERROR,
                    )
        loop = asyncio.get_running_loop()
        executor = self.sync_executor
        return await loop.run_in_executor(
            executor,
            lambda: self.execute_tool(call, allow_tools=allow_tools, budget=budget, turn_id=turn_id),
        )

    def _apply_tool_budget(
        self,
        call: CanonicalToolCall,
        result: CanonicalToolResult,
        *,
        budget: ToolCallBudget | None,
    ) -> CanonicalToolResult:
        if budget is None:
            return result
        normalized = self._apply_shell_exec_budget(call, result, budget=budget)
        rendered = self._render_result_payload(normalized)
        original_size = len(rendered)
        char_limit = self._resolve_char_limit(budget)
        if char_limit is None or original_size <= char_limit:
            return normalized
        preview_chars = max(256, int(budget.preview_chars or 1000))
        preview_text = rendered[: min(preview_chars, char_limit)].rstrip()
        artifact = None
        if budget.max_result_spill_chars is not None and original_size > budget.max_result_spill_chars:
            artifact = self._spill_tool_result(call, normalized, rendered, budget=budget)
        lines = [preview_text] if preview_text else []
        marker = f"[truncated: original={original_size} chars, kept={len(preview_text)} chars]"
        lines.append(marker)
        if artifact is not None:
            lines.append(f"[artifact: {artifact.local_cached_path}]")
        preview_payload = "\n\n".join(part for part in lines if part).strip() or marker
        structured = dict(normalized.structured or {})
        structured.update(
            {
                "truncated": True,
                "original_size": original_size,
                "preview_size": len(preview_text),
                "max_output_chars": char_limit,
                "max_output_tokens_estimate": budget.max_output_tokens_estimate,
                "artifact_ref": self._artifact_ref(artifact),
            }
        )
        return CanonicalToolResult(
            name=normalized.name,
            ok=normalized.ok,
            text=preview_payload,
            structured=structured,
            call_id=normalized.call_id,
            llm_text=preview_payload,
            status=normalized.status,
        )

    def _apply_shell_exec_budget(
        self,
        call: CanonicalToolCall,
        result: CanonicalToolResult,
        *,
        budget: ToolCallBudget,
    ) -> CanonicalToolResult:
        if call.name != "op_exec_shell":
            return result
        structured = dict(result.structured or {})
        max_lines = budget.max_lines_to_read
        max_stdout_chars = budget.max_stdout_chars
        changed = False
        for key in ("stdout", "stderr", "display_text"):
            value = structured.get(key)
            if not isinstance(value, str):
                continue
            truncated_value, line_truncated = _truncate_linewise(value, max_lines=max_lines)
            if max_stdout_chars is not None and len(truncated_value) > max_stdout_chars:
                truncated_value = truncated_value[:max_stdout_chars].rstrip()
                line_truncated = True
            if truncated_value != value:
                structured[key] = truncated_value
                structured[f"{key}_truncated"] = True
                if line_truncated and max_lines is not None:
                    structured[f"{key}_line_limit"] = max_lines
                changed = True
        if not changed:
            return result
        display_text = str(structured.get("display_text") or result.text or result.llm_text or "").strip()
        if not display_text:
            display_text = result.text or result.llm_text
        return CanonicalToolResult(
            name=result.name,
            ok=result.ok,
            text=display_text,
            structured=structured,
            call_id=result.call_id,
            llm_text=display_text,
            status=result.status,
        )

    def _resolve_char_limit(self, budget: ToolCallBudget) -> int | None:
        candidates = [value for value in (budget.max_output_chars, budget.max_stdout_chars) if isinstance(value, int) and value > 0]
        if not candidates:
            return None
        return min(candidates)

    @staticmethod
    def _render_result_payload(result: CanonicalToolResult) -> str:
        if str(result.llm_text or "").strip():
            return str(result.llm_text).strip()
        if str(result.text or "").strip():
            return str(result.text).strip()
        if result.structured:
            return json.dumps(result.structured, ensure_ascii=False, sort_keys=True)
        return ""

    def _spill_tool_result(
        self,
        call: CanonicalToolCall,
        result: CanonicalToolResult,
        rendered: str,
        *,
        budget: ToolCallBudget,
    ) -> StoredArtifact | None:
        if self.runtime_root is None:
            return None
        ingestor = ArtifactIngestor(self.runtime_root)
        payload = json.dumps(
            {
                "tool_name": call.name,
                "call_id": getattr(call, "call_id", None),
                "args": dict(call.args),
                "status": result.status,
                "ok": result.ok,
                "text": result.text,
                "llm_text": result.llm_text,
                "structured": result.structured,
                "rendered": rendered,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        return ingestor.store_bytes(
            channel_kind="tool_results",
            bucket_id=str(budget.artifact_bucket_id or getattr(call, "call_id", None) or call.name or "tool"),
            file_name=f"{call.name.replace('/', '_')}.json",
            content=payload,
            mime_type="application/json",
        )

    @staticmethod
    def _artifact_ref(artifact: StoredArtifact | None) -> dict[str, Any] | None:
        if artifact is None:
            return None
        return {
            "artifact_id": artifact.artifact_id,
            "local_cached_path": artifact.local_cached_path,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "mime_type": artifact.mime_type,
        }

    def call_registered(self, call: CapabilityCall) -> CapabilityResult:
        target_id = str(call.args.get("target_id") or SINGLETON_TARGET)
        bound = self.bound_action_index.get(call.name, target_id)
        if bound is not None:
            lifecycle_result = self._maybe_handle_lifecycle_action(bound.descriptor)
            if lifecycle_result is not None:
                return lifecycle_result
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
                return _target_id_required_result(canonical_path=call.name, available_target_ids=instance_targets)
        registered = self.capabilities.get(call.name)
        if registered is None:
            resolved = self._resolve_descriptor(call.name, target_id=target_id)
            if isinstance(resolved, CapabilityResult):
                return resolved
            if resolved is not None:
                registered = self.capabilities.get(resolved.name)
        if registered is None:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text=f"unknown capability: {call.name}",
                llm_text=f"unknown capability: {call.name}",
            )
        lifecycle_result = self._maybe_handle_lifecycle_action(registered.descriptor)
        if lifecycle_result is not None:
            return lifecycle_result
        return registered.callable(call)

    def _maybe_handle_lifecycle_action(self, descriptor: CapabilityDescriptor) -> CapabilityResult | None:
        if not descriptor.detachable:
            return None
        metadata = dict(descriptor.metadata or {})
        if metadata.get("namespace") != "operation":
            return None
        action = str(metadata.get("action") or "").strip()
        if action not in {"attach", "detach"}:
            return None
        if descriptor.target_id and descriptor.target_id != SINGLETON_TARGET:
            return None
        controller = self.lifecycle_controller
        if controller is None:
            return None
        module_id = str(descriptor.module_id or "").strip()
        if not module_id:
            return None
        if action == "detach":
            status = controller.detach_module(module_id)
            verb = "detached"
        else:
            status = controller.reattach_module(module_id)
            verb = "attached"
        payload = {
            "module_id": module_id,
            "action": action,
            "status": status,
            "lifecycle_controller": "core",
        }
        return CapabilityResult(
            status=status,
            text=f"module {verb}: {module_id}",
            structured=payload,
            llm_text=f"Module {module_id} {verb} via core lifecycle.",
        )

    def _resolve_descriptor(self, name: str, *, target_id: str = SINGLETON_TARGET) -> CapabilityDescriptor | CapabilityResult | None:
        candidates: list[CapabilityDescriptor] = []
        if name in self.compiled_capability_index.by_canonical:
            candidates.extend(
                self.compiled_capability_index.records[record_id]
                for record_id in self.compiled_capability_index.by_canonical[name]
                if record_id in self.compiled_capability_index.records
            )
        if name in self.compiled_capability_index.aliases:
            candidates.extend(
                self.compiled_capability_index.records[record_id]
                for record_id in self.compiled_capability_index.aliases[name]
                if record_id in self.compiled_capability_index.records
            )
        if not candidates:
            return None
        unique: dict[str, CapabilityDescriptor] = {descriptor.name: descriptor for descriptor in candidates}
        candidates = list(unique.values())
        if target_id != SINGLETON_TARGET:
            targeted = [descriptor for descriptor in candidates if (descriptor.target_id or SINGLETON_TARGET) == target_id]
            if len(targeted) == 1:
                return targeted[0]
            if len(targeted) > 1:
                return CapabilityResult(
                    status=RuntimeStatus.INVALID,
                    text="capability alias is ambiguous",
                    structured={"name": name, "target_id": target_id, "matches": [item.name for item in targeted]},
                    llm_text="capability alias is ambiguous",
                )
        singleton = [descriptor for descriptor in candidates if (descriptor.target_id or SINGLETON_TARGET) == SINGLETON_TARGET]
        if len(singleton) == 1:
            return singleton[0]
        if len(singleton) > 1:
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="capability alias is ambiguous",
                structured={"name": name, "matches": [item.name for item in singleton]},
                llm_text="capability alias is ambiguous",
            )
        instance_targets = sorted(
            {
                descriptor.target_id
                for descriptor in candidates
                if descriptor.target_id and descriptor.target_id != SINGLETON_TARGET
            }
        )
        if instance_targets:
            return _target_id_required_result(name=name, available_target_ids=instance_targets)
        return None

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
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.sync_executor, lambda: self.execute(call))

    async def interrupt_turn(self, turn_id: str) -> None:
        if not turn_id:
            return
        with self._interrupt_state_lock:
            task = self._interrupt_tasks.get(turn_id)
            if task is None or task.done():
                task = asyncio.create_task(self._interrupt_turn_handles_async(turn_id))
                self._interrupt_tasks[turn_id] = task
        try:
            await task
        finally:
            with self._interrupt_state_lock:
                if self._interrupt_tasks.get(turn_id) is task and task.done():
                    self._interrupt_tasks.pop(turn_id, None)

    async def _interrupt_turn_handles_async(self, turn_id: str) -> None:
        while True:
            with self._interrupt_state_lock:
                handles = list(self._interrupt_handles.get(turn_id, set()))
            if not handles:
                with self._interrupt_state_lock:
                    self._interrupt_handles.pop(turn_id, None)
                return
            for handle in handles:
                cancel = getattr(handle, "cancel", None)
                if callable(cancel):
                    with contextlib.suppress(Exception):
                        result = cancel()
                        if asyncio.iscoroutine(result):
                            await result
                with self._interrupt_state_lock:
                    bucket = self._interrupt_handles.get(turn_id)
                    if bucket:
                        bucket.discard(handle)
                        if not bucket:
                            self._interrupt_handles.pop(turn_id, None)

    def register_interrupt_handle(self, turn_id: str | None, handle: Any) -> None:
        if not turn_id:
            return
        with self._interrupt_state_lock:
            bucket = self._interrupt_handles.setdefault(turn_id, set())
            bucket.add(handle)

    def release_interrupt_handle(self, turn_id: str | None, handle: Any) -> None:
        if not turn_id:
            return
        with self._interrupt_state_lock:
            bucket = self._interrupt_handles.get(turn_id)
            if not bucket:
                return
            bucket.discard(handle)
            if not bucket:
                self._interrupt_handles.pop(turn_id, None)


def _truncate_linewise(text: str, *, max_lines: int | None) -> tuple[str, bool]:
    if not isinstance(max_lines, int) or max_lines <= 0:
        return text, False
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    kept = "\n".join(lines[:max_lines]).rstrip()
    suffix = f"\n\n[... truncated after {max_lines} lines, original: {len(lines)} lines]"
    return f"{kept}{suffix}".strip(), True


def _target_id_required_result(
    *,
    available_target_ids: list[str],
    canonical_path: str = "",
    name: str = "",
) -> CapabilityResult:
    payload = {
        "error_code": "target_id_required",
        "available_target_ids": list(available_target_ids),
    }
    if canonical_path:
        payload["canonical_path"] = canonical_path
    if name:
        payload["name"] = name
    target_text = ", ".join(available_target_ids) if available_target_ids else "(none)"
    capability = canonical_path or name or "this capability"
    return CapabilityResult(
        status=RuntimeStatus.INVALID,
        text="target_id is required for this capability",
        structured=payload,
        llm_text=(
            f"target_id is required for {capability}. "
            f"Available target_id values: {target_text}. "
            "Retry with args.target_id set to one of these values."
        ),
    )


def _capability_spec_payload(descriptor: CapabilityDescriptor) -> dict[str, Any]:
    canonical = descriptor.canonical_path or descriptor.name
    display = descriptor.display_name or canonical
    call_names: list[str] = []
    for value in (canonical, descriptor.name, display, *descriptor.aliases):
        normalized = str(value or "").strip()
        if normalized and normalized not in call_names:
            call_names.append(normalized)
    return {
        "canonical_path": canonical,
        "name": canonical,
        "display_name": display,
        "family": descriptor.family,
        "description": descriptor.description,
        "module_id": descriptor.module_id,
        "call_names": call_names,
        "aliases": list(descriptor.aliases),
        "parameters_schema": dict(descriptor.parameters_schema or {"type": "object", "properties": {}}),
        "result_schema": dict(descriptor.result_schema or {"type": "object", "properties": {}}),
        "source": descriptor.source,
        "target_kind": descriptor.target_kind,
        "target_id": descriptor.target_id,
        "metadata": dict(descriptor.metadata or {}),
    }
