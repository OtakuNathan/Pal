from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

from pal.shared.tool_protocol import new_tool_call

import asyncio
import contextlib
import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ValidationError

from pal.execution.capability_compiler import compile_provider_subtree
from pal.execution.contracts import (
    CapabilityCall,
    CapabilityDescriptor,
    CapabilityResult,
    ExecutionRuntimePort,
    ToolCallBudget,
)
from pal.execution.tool_facade import (
    CompleteResult,
    EffectKind,
    EffectOutcome,
    EffectReceipt,
    FailedResult,
    InvocationMode,
    McpToolOutput,
    PagingMode,
    PagedResult,
    ProviderPayloadOutput,
    RejectedResult,
    RetryDirective,
    ToolAffordance,
    ToolHandlerResult,
    ToolInvocationResult,
    ToolRejectedError,
    derive_retry_directive,
    rejection,
    validate_output,
)
from pal.execution.tool_registry import (
    CompiledToolRecord,
    ToolRegistryGeneration,
    compile_registry_generation,
)
from pal.shared import ToolExecutionResult
from pal.plugins.l3.registry import L3PluginRegistry
from pal.plugins.l3.stubs import NullL3Plugin
from pal.execution.tool_result_pager import (
    DEFAULT_TOOL_RESULT_RETENTION_USER_TURNS,
    ToolResultPage,
    ToolResultPagerStore,
)
from pal.execution.session_state import (
    FileDeliveryManifest,
    FileDeliverySpan,
    InMemoryLogicalExecutionState,
    LogicalExecutionContext,
    LogicalExecutionStateBackend,
    projection_hash,
)
from pal.shared import (
    BoundCapabilityAction,
    MountedSubtreeHandle,
    RuntimeStatus,
    SINGLETON_TARGET,
)
from pal.shared.text_search import jieba_search_terms

if TYPE_CHECKING:
    from pal.core.module_registry import ModuleHandle


def _merge_explicit_model_fields(
    defaults: dict[str, Any],
    explicit: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in explicit.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_explicit_model_fields(
                dict(merged[key]),
                value,
            )
        else:
            merged[key] = value
    return merged


def _invocation_args(
    validated: BaseModel | dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(validated, BaseModel):
        return dict(validated)
    defaults = validated.model_dump(mode="python", exclude_none=True)
    explicit = validated.model_dump(mode="python", exclude_unset=True)
    return _merge_explicit_model_fields(defaults, explicit)


@dataclass
class ExecutionRuntime(ExecutionRuntimePort):
    provider_registry: dict[str, Any] = field(default_factory=dict)
    l3_plugin_registry: L3PluginRegistry = field(default_factory=L3PluginRegistry)
    runtime_root: Path | None = None
    logical_state: LogicalExecutionStateBackend = field(
        default_factory=InMemoryLogicalExecutionState
    )
    tool_result_pager: ToolResultPagerStore = field(default_factory=ToolResultPagerStore)
    lifecycle_controller: Any | None = None
    sync_executor_max_workers: int = 4
    sync_executor: ThreadPoolExecutor | None = None
    _interrupt_handles: dict[str, set[Any]] = field(default_factory=dict)
    _interrupt_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _interrupt_state_lock: threading.Lock = field(default_factory=threading.Lock)
    _registry_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _registry_generation: ToolRegistryGeneration = field(
        default_factory=ToolRegistryGeneration.empty,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.tool_result_pager.state_backend = self.logical_state
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
        with self._interrupt_state_lock:
            handles = {
                handle
                for bucket in self._interrupt_handles.values()
                for handle in bucket
            }
            self._interrupt_handles.clear()
            self._interrupt_tasks.clear()
        for handle in handles:
            terminate = getattr(handle, "terminate", None)
            if callable(terminate):
                with contextlib.suppress(Exception):
                    terminate()
        if self.sync_executor is not None:
            self.sync_executor.shutdown(wait=False, cancel_futures=True)
            self.sync_executor = None

    @property
    def registry_generation(self) -> ToolRegistryGeneration:
        return self._registry_generation

    @property
    def capability_registry(self):
        return self._registry_generation.capability_registry

    @property
    def capability_forest(self):
        return self._registry_generation.forest

    @property
    def compiled_capability_index(self):
        return self._registry_generation.capability_index

    @property
    def bound_action_index(self):
        return self._registry_generation.canonical_bindings

    def register_provider_ref(self, provider_id: str, provider: Any) -> None:
        self.provider_registry[provider_id] = provider

    def unregister_provider_ref(self, provider_id: str) -> None:
        self.provider_registry.pop(provider_id, None)

    def begin_tool_result_turn(
        self,
        *,
        turn_id: str,
        scope_key: str = "",
        retention_user_turns: int = DEFAULT_TOOL_RESULT_RETENTION_USER_TURNS,
        input_id: str = "",
    ) -> LogicalExecutionContext:
        return self.tool_result_pager.begin_turn(
            runtime_root=self.runtime_root,
            turn_id=turn_id,
            scope_key=scope_key,
            retention_user_turns=retention_user_turns,
            input_id=input_id,
        )

    def read_tool_result_page(
        self,
        *,
        result_ref: str,
        page: int = 1,
        page_size: int | None = None,
        anchor: str = "head",
        turn_id: str | None = None,
        logical_session_id: str = "",
    ) -> ToolResultPage | None:
        return self.tool_result_pager.read_page(
            result_ref,
            page=page,
            page_size=page_size,
            anchor=anchor,
            turn_id=turn_id,
            logical_session_id=logical_session_id,
        )

    def logical_context_for_turn(self, turn_id: str | None) -> LogicalExecutionContext:
        return self.tool_result_pager.context_for_turn(turn_id)

    def reconcile_tool_context(
        self,
        *,
        turn_id: str | None,
        original_messages: list[dict[str, Any]],
        projected_messages: list[dict[str, Any]],
        delivery_records: dict[str, dict[str, Any]],
    ) -> LogicalExecutionContext:
        context = self.logical_context_for_turn(turn_id)
        original_by_call = {
            str(message.get("tool_call_id") or ""): str(message.get("content") or "")
            for message in original_messages
            if str(message.get("role") or "") == "tool"
        }
        projection: list[str] = []
        deliveries: list[dict[str, Any]] = []
        for message in projected_messages:
            if str(message.get("role") or "") != "tool":
                continue
            call_id = str(message.get("tool_call_id") or "")
            content = str(message.get("content") or "")
            projection.append(projection_hash(call_id, content))
            manifest = FileDeliveryManifest.from_dict(delivery_records.get(call_id))
            source = original_by_call.get(call_id, "")
            if manifest is None or not source:
                continue
            raw_ranges = message.get("_pal_visible_source_ranges")
            source_ranges = (
                tuple(
                    (max(0, int(item[0])), max(0, int(item[1])))
                    for item in list(raw_ranges or ())
                    if isinstance(item, (list, tuple)) and len(item) == 2
                )
                if raw_ranges is not None
                else ((0, len(source)),)
            )
            visible_spans_list: list[FileDeliverySpan] = []
            for span in manifest.spans:
                for range_start, range_end in source_ranges:
                    visible_start = max(span.start_offset, range_start)
                    visible_end = min(span.end_offset, range_end)
                    if visible_end <= visible_start:
                        continue
                    visible_spans_list.append(
                        FileDeliverySpan(
                            start_offset=visible_start,
                            end_offset=visible_end,
                            start_line=span.start_line,
                            end_line=span.end_line,
                            visible_start_in_line=(
                                span.visible_start_in_line
                                + visible_start
                                - span.start_offset
                            ),
                            visible_end_in_line=(
                                span.visible_start_in_line
                                + visible_end
                                - span.start_offset
                            ),
                            line_length=span.line_length,
                        )
                    )
            visible_spans = tuple(visible_spans_list)
            empty_visible = (
                manifest.empty_file
                and any(
                    range_start <= source.find(manifest.empty_marker)
                    and range_end
                    >= source.find(manifest.empty_marker)
                    + len(manifest.empty_marker)
                    for range_start, range_end in source_ranges
                )
                and manifest.empty_marker in source
            )
            if not visible_spans and not empty_visible:
                continue
            deliveries.append(
                FileDeliveryManifest(
                    file_key=manifest.file_key,
                    digest=manifest.digest,
                    total_lines=manifest.total_lines,
                    spans=visible_spans,
                    empty_file=empty_visible,
                    empty_marker=manifest.empty_marker,
                ).to_dict()
            )
        return self.logical_state.reconcile_projection(
            logical_session_id=context.logical_session_id,
            projection=tuple(projection),
            deliveries=tuple(deliveries),
        )

    def commit_tool_delivery(
        self,
        *,
        turn_id: str | None,
        context_delivery: dict[str, Any] | None,
    ) -> LogicalExecutionContext:
        """Commit a tool delivery after its result has entered L1."""

        context = self.logical_context_for_turn(turn_id)
        manifest = FileDeliveryManifest.from_dict(context_delivery)
        if manifest is None:
            return context
        return self.logical_state.record_delivery(
            logical_session_id=context.logical_session_id,
            delivery=manifest.to_dict(),
        )

    def list_tool_specs(self) -> list[dict[str, Any]]:
        generation = self._registry_generation
        records = {**generation.direct_aliases, **generation.indirect_aliases}
        return [self._tool_spec_from_record(records[alias]) for alias in sorted(records)]

    def get_tool_spec(self, name: str) -> dict[str, Any] | None:
        record = self._registry_generation.record_for_alias(str(name or "").strip())
        if record is None:
            return None
        return self._tool_spec_from_record(record)

    @staticmethod
    def _tool_spec_from_record(record: CompiledToolRecord) -> dict[str, Any]:
        return {
            "name": record.alias,
            "display_name": record.alias,
            "family": record.family or "general",
            "description": record.description,
            "search_text": record.search_text,
            "invocation_mode": record.execution.invocation_mode.value,
            "input_schema": dict(record.input_schema),
            "output_schema": dict(record.output_schema),
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
        return None

    def has_registered_capability(self, name: str) -> bool:
        canonical_path = self.resolve_capability_address(name)
        return bool(self.compiled_capability_index.by_canonical.get(canonical_path))

    def _first_descriptor_match(self, name: str) -> CapabilityDescriptor | None:
        raw = str(name or "").strip()
        direct = self.compiled_capability_index.records.get(raw)
        if direct is not None:
            return direct
        canonical_path = self.resolve_capability_address(raw)
        for record_id in self.compiled_capability_index.by_canonical.get(canonical_path, []):
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
        with self._registry_lock:
            if subtree.mounted:
                return [descriptor.name for descriptor in subtree.descriptors]
            current = self._registry_generation
            prepared = self._prepared_subtree(subtree)
            mounted = dict(current.mounted_subtrees)
            mounted[subtree.module_id] = prepared
            candidate = compile_registry_generation(
                generation_id=current.generation_id + 1,
                mounted_subtrees=mounted,
            )
            self._registry_generation = candidate
            subtree.mounted = True
        return [descriptor.name for descriptor in subtree.descriptors]

    def unmount_subtree(self, handle: "ModuleHandle") -> list[str]:
        subtree = handle.mounted_subtree
        if subtree is None:
            return []
        with self._registry_lock:
            if not subtree.mounted:
                return []
            current = self._registry_generation
            mounted = dict(current.mounted_subtrees)
            mounted.pop(subtree.module_id, None)
            candidate = compile_registry_generation(
                generation_id=current.generation_id + 1,
                mounted_subtrees=mounted,
            )
            self._registry_generation = candidate
            subtree.mounted = False
        return list(subtree.search_record_ids)

    def _prepared_subtree(self, subtree: MountedSubtreeHandle) -> MountedSubtreeHandle:
        prepared = MountedSubtreeHandle(module_id=subtree.module_id)
        prepared.nodes.extend(subtree.nodes)
        prepared.descriptors.extend(subtree.descriptors)
        prepared.bound_actions.extend(subtree.bound_actions)
        prepared.node_ids.extend(subtree.node_ids)
        prepared.bound_action_keys.extend(subtree.bound_action_keys)
        prepared.search_record_ids.extend(subtree.search_record_ids)
        return prepared

    def invoke_direct_tool(
        self,
        call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
        generation: ToolRegistryGeneration | None = None,
    ) -> ToolInvocationResult:
        captured = generation or self._registry_generation
        return self._invoke_tool_record_sync(
            captured,
            call,
            invocation_mode=InvocationMode.DIRECT,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
        )

    def invoke_indirect_tool(
        self,
        call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
        generation: ToolRegistryGeneration | None = None,
    ) -> ToolInvocationResult:
        captured = generation or self._registry_generation
        return self._invoke_tool_record_sync(
            captured,
            call,
            invocation_mode=InvocationMode.INDIRECT,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
        )

    async def invoke_direct_tool_async(
        self,
        call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
        generation: ToolRegistryGeneration | None = None,
    ) -> ToolInvocationResult:
        captured = generation or self._registry_generation
        return await self._invoke_tool_record_async(
            captured,
            call,
            invocation_mode=InvocationMode.DIRECT,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
        )

    async def invoke_indirect_tool_async(
        self,
        call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
        generation: ToolRegistryGeneration | None = None,
    ) -> ToolInvocationResult:
        captured = generation or self._registry_generation
        return await self._invoke_tool_record_async(
            captured,
            call,
            invocation_mode=InvocationMode.INDIRECT,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
        )

    def _invoke_tool_record_sync(
        self,
        generation: ToolRegistryGeneration,
        call: ToolCallIR,
        *,
        invocation_mode: InvocationMode,
        allow_tools: bool,
        budget: ToolCallBudget | None,
        turn_id: str | None,
    ) -> ToolInvocationResult:
        resolved = self._resolve_invocation_record(generation, call, invocation_mode=invocation_mode)
        if isinstance(resolved, RejectedResult):
            return resolved
        record = resolved
        validated = self._validate_invocation_input(record, call.args)
        if isinstance(validated, RejectedResult):
            return validated
        special = self._invoke_facade_builtin_sync(
            generation,
            record,
            call,
            validated,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
        )
        if special is not None:
            return special
        if not allow_tools:
            return rejection(
                "finalization_only",
                "tool execution disabled in finalization mode",
                retry=RetryDirective.DO_NOT_RETRY,
            )
        lifecycle = self._maybe_handle_lifecycle_action(record.binding.descriptor)
        try:
            raw = lifecycle if lifecycle is not None else self._call_record_sync(record, call, validated, turn_id, budget, allow_tools)
            return self._normalize_invocation_result(
                record,
                call,
                raw,
                budget=budget,
                turn_id=turn_id,
            )
        except ToolRejectedError as exc:
            return self._rejected_error_result(exc)
        except Exception as exc:
            return self._handler_exception_result(record, exc)

    async def _invoke_tool_record_async(
        self,
        generation: ToolRegistryGeneration,
        call: ToolCallIR,
        *,
        invocation_mode: InvocationMode,
        allow_tools: bool,
        budget: ToolCallBudget | None,
        turn_id: str | None,
    ) -> ToolInvocationResult:
        resolved = self._resolve_invocation_record(generation, call, invocation_mode=invocation_mode)
        if isinstance(resolved, RejectedResult):
            return resolved
        record = resolved
        validated = self._validate_invocation_input(record, call.args)
        if isinstance(validated, RejectedResult):
            return validated
        special = await self._invoke_facade_builtin_async(
            generation,
            record,
            call,
            validated,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
        )
        if special is not None:
            return special
        if not allow_tools:
            return rejection(
                "finalization_only",
                "tool execution disabled in finalization mode",
                retry=RetryDirective.DO_NOT_RETRY,
            )
        lifecycle = self._maybe_handle_lifecycle_action(record.binding.descriptor)
        try:
            raw = lifecycle if lifecycle is not None else await self._call_record_async(
                record, call, validated, turn_id, budget, allow_tools
            )
            return self._normalize_invocation_result(
                record,
                call,
                raw,
                budget=budget,
                turn_id=turn_id,
            )
        except ToolRejectedError as exc:
            return self._rejected_error_result(exc)
        except Exception as exc:
            return self._handler_exception_result(record, exc)

    @staticmethod
    def _resolve_invocation_record(
        generation: ToolRegistryGeneration,
        call: ToolCallIR,
        *,
        invocation_mode: InvocationMode,
    ) -> CompiledToolRecord | RejectedResult:
        alias = str(call.name or "").strip()
        expected = generation.direct_aliases if invocation_mode is InvocationMode.DIRECT else generation.indirect_aliases
        wrong = generation.indirect_aliases if invocation_mode is InvocationMode.DIRECT else generation.direct_aliases
        record = expected.get(alias)
        if record is not None:
            return record
        wrong_record = wrong.get(alias)
        if wrong_record is not None:
            if invocation_mode is InvocationMode.DIRECT:
                affordance = ToolAffordance(
                    tool="call_tool",
                    arguments={"name": alias, "args": dict(call.args)},
                    reason="This tool is indirect and must be invoked through call_tool.",
                )
            else:
                affordance = ToolAffordance(
                    tool=alias,
                    arguments=dict(call.args),
                    reason="This tool is direct and must be invoked as a provider tool.",
                )
            return rejection(
                "wrong_invocation_mode",
                f"tool {alias!r} uses {wrong_record.execution.invocation_mode.value} invocation",
                retry=RetryDirective.CORRECT_INPUT,
                affordances=[affordance],
                details={"correct_invocation_mode": wrong_record.execution.invocation_mode.value},
            )
        return rejection(
            "unknown_tool",
            f"unknown tool alias: {alias}",
            retry=RetryDirective.CORRECT_INPUT,
            affordances=[
                ToolAffordance(
                    tool="search_tools",
                    arguments={"query": alias},
                    reason="Aliases are generation-scoped; search the current registry instead of guessing a canonical path.",
                )
            ],
        )

    @staticmethod
    def _validate_invocation_input(
        record: CompiledToolRecord,
        args: dict[str, Any],
    ) -> BaseModel | dict[str, Any] | RejectedResult:
        try:
            if record.is_mcp:
                Draft202012Validator(record.input_schema).validate(dict(args or {}))
                return dict(args or {})
            if record.input_model is None:
                raise TypeError("internal tool has no InputModel")
            return record.input_model.model_validate(dict(args or {}), strict=True)
        except (ValidationError, JsonSchemaValidationError, TypeError) as exc:
            details = (
                {"validation_errors": exc.errors(include_url=False, include_input=False)}
                if isinstance(exc, ValidationError)
                else {"validation_error": str(exc)}
            )
            return rejection(
                "invalid_arguments",
                f"invalid arguments for {record.alias}: {exc}",
                retry=RetryDirective.CORRECT_INPUT,
                affordances=[
                    ToolAffordance(
                        tool="read_tool",
                        arguments={"name": record.alias},
                        reason="Read the bound input schema and valid example before correcting the call.",
                    )
                ],
                details=details,
            )

    def _invoke_facade_builtin_sync(
        self,
        generation: ToolRegistryGeneration,
        record: CompiledToolRecord,
        call: ToolCallIR,
        validated: BaseModel | dict[str, Any],
        *,
        allow_tools: bool,
        budget: ToolCallBudget | None,
        turn_id: str | None,
    ) -> ToolInvocationResult | None:
        args = validated.model_dump(mode="python") if isinstance(validated, BaseModel) else dict(validated)
        if record.alias == "search_tools":
            return self._complete_builtin(record, self._search_generation(generation, args))
        if record.alias == "read_tool":
            payload = self._read_generation_tool(generation, str(args.get("name") or ""))
            if payload is None:
                return rejection(
                    "unknown_tool",
                    f"unknown tool alias: {args.get('name')}",
                    affordances=[ToolAffordance(tool="search_tools", arguments={"query": args.get("name") or ""}, reason="Search current aliases.")],
                )
            return self._complete_builtin(record, payload)
        if record.alias == "call_tool":
            target = new_tool_call(
                call_id=call.call_id,
                name=str(args.get("name") or ""),
                arguments=dict(args.get("args") or {}),
            )
            return self._invoke_tool_record_sync(
                generation,
                target,
                invocation_mode=InvocationMode.INDIRECT,
                allow_tools=allow_tools,
                budget=budget,
                turn_id=turn_id,
            )
        if record.alias == "read_tool_result":
            return self._read_tool_result_builtin(
                record,
                call,
                args,
                turn_id=turn_id,
            )
        return None

    async def _invoke_facade_builtin_async(
        self,
        generation: ToolRegistryGeneration,
        record: CompiledToolRecord,
        call: ToolCallIR,
        validated: BaseModel | dict[str, Any],
        *,
        allow_tools: bool,
        budget: ToolCallBudget | None,
        turn_id: str | None,
    ) -> ToolInvocationResult | None:
        args = validated.model_dump(mode="python") if isinstance(validated, BaseModel) else dict(validated)
        if record.alias == "search_tools":
            return self._complete_builtin(record, self._search_generation(generation, args))
        if record.alias == "read_tool":
            payload = self._read_generation_tool(generation, str(args.get("name") or ""))
            if payload is None:
                return rejection(
                    "unknown_tool",
                    f"unknown tool alias: {args.get('name')}",
                    affordances=[ToolAffordance(tool="search_tools", arguments={"query": args.get("name") or ""}, reason="Search current aliases.")],
                )
            return self._complete_builtin(record, payload)
        if record.alias == "call_tool":
            target = new_tool_call(
                call_id=call.call_id,
                name=str(args.get("name") or ""),
                arguments=dict(args.get("args") or {}),
            )
            return await self._invoke_tool_record_async(
                generation,
                target,
                invocation_mode=InvocationMode.INDIRECT,
                allow_tools=allow_tools,
                budget=budget,
                turn_id=turn_id,
            )
        if record.alias == "read_tool_result":
            return self._read_tool_result_builtin(
                record,
                call,
                args,
                turn_id=turn_id,
            )
        return None

    def _read_tool_result_builtin(
        self,
        record: CompiledToolRecord,
        call: ToolCallIR,
        args: dict[str, Any],
        *,
        turn_id: str | None,
    ) -> ToolInvocationResult:
        raw = record.binding.callable(
            CapabilityCall(
                name=record.canonical_path,
                args=args,
                meta={
                    "tool_call": call,
                    "turn_id": str(turn_id or ""),
                    "execution_runtime": self,
                },
            )
        )
        if not isinstance(raw, CapabilityResult):
            return FailedResult(
                error_code="invalid_pager_result",
                error="read_tool_result returned an invalid internal result",
                effect=EffectOutcome.NONE,
                retry=RetryDirective.DO_NOT_RETRY,
                llm_text="read_tool_result returned an invalid internal result",
            )
        if raw.status != RuntimeStatus.OK:
            details = dict(raw.structured or {})
            reason = str(details.get("reason") or "expired_handle")
            affordances = self._pager_recovery_affordances(details)
            return FailedResult(
                error_code=reason,
                error=raw.text,
                effect=EffectOutcome.NONE,
                retry=RetryDirective.DO_NOT_RETRY,
                llm_text=raw.llm_text,
                details=details,
                affordances=affordances,
            )
        output = {**dict(raw.structured or {}), "page_text": raw.text}
        affordances: list[ToolAffordance] = []
        result_ref = str(output.get("result_ref") or "")
        page = int(output.get("anchor_page") or output.get("page") or 1)
        anchor = str(output.get("anchor") or "head")
        if bool(output.get("has_more_after")):
            next_page = page - 1 if anchor == "tail" else page + 1
            affordances.append(
                ToolAffordance(
                    tool="read_tool_result",
                    arguments={"result_ref": result_ref, "page": next_page, "anchor": anchor},
                    reason="Read the exact adjacent newer/next page.",
                )
            )
        if bool(output.get("has_more_before")):
            previous_page = page + 1 if anchor == "tail" else max(1, page - 1)
            affordances.append(
                ToolAffordance(
                    tool="read_tool_result",
                    arguments={"result_ref": result_ref, "page": previous_page, "anchor": anchor},
                    reason="Read the exact adjacent older/previous page.",
                )
            )
        return self._complete_builtin(
            record,
            output,
            llm_text=raw.llm_text,
            affordances=affordances,
            context_delivery=raw.context_delivery,
        )

    def _pager_recovery_affordances(
        self,
        details: dict[str, Any],
    ) -> list[ToolAffordance]:
        origin = dict(details.get("origin") or {})
        alias = str(origin.get("alias") or "")
        arguments = dict(origin.get("arguments") or {})
        invocation_mode = str(origin.get("invocation_mode") or "")
        execution = dict(origin.get("execution") or {})
        retry_policy = str(execution.get("retry_policy") or "")
        idempotency = str(execution.get("idempotency") or "")
        current = self.registry_generation.record_for_alias(alias) if alias else None
        if (
            current is not None
            and retry_policy == "automatic"
            and idempotency == "idempotent"
        ):
            if invocation_mode == InvocationMode.INDIRECT.value:
                return [
                    ToolAffordance(
                        tool="call_tool",
                        arguments={"name": alias, "args": arguments},
                        reason="The materialized result expired; rerun the original idempotent read.",
                    )
                ]
            return [
                ToolAffordance(
                    tool=alias,
                    arguments=arguments,
                    reason="The materialized result expired; rerun the original idempotent read.",
                )
            ]
        query = str(origin.get("search_text") or alias or "original tool")
        return [
            ToolAffordance(
                tool="search_tools",
                arguments={"query": query},
                reason=(
                    "The handle expired. Rediscover the current tool and inspect its retry semantics; "
                    "do not automatically repeat an effectful or non-idempotent call."
                ),
            )
        ]

    @staticmethod
    def _search_generation(generation: ToolRegistryGeneration, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip().lower()
        namespace = str(args.get("namespace") or "").strip().lower()
        namespace = {"inspect": "introspection", "action": "operation"}.get(namespace, namespace)
        family = str(args.get("family") or "").strip().lower()
        module_id = str(args.get("module_id") or "").strip().lower()
        tags = {
            str(item).strip().lower()
            for item in list(args.get("tags") or ())
            if str(item).strip()
        }
        include_facets = bool(args.get("facets", False))
        try:
            limit = max(1, int(args.get("top_k") or args.get("limit") or 10))
        except (TypeError, ValueError):
            limit = 10
        terms = tuple(item.lower() for item in jieba_search_terms(query))
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for alias, item in generation.search_records.items():
            item_namespace = str(item.get("namespace") or "").lower()
            item_family = str(item.get("family") or "").lower()
            item_module = str(item.get("module_id") or "").lower()
            item_tags = {str(tag).lower() for tag in item.get("tags", ())}
            if namespace and item_namespace != namespace:
                continue
            if family and item_family != family:
                continue
            if module_id and item_module != module_id:
                continue
            if tags and not tags.issubset(item_tags):
                continue
            alias_text = alias.lower()
            search_text = str(item["search_text"]).lower()
            haystack = f"{alias_text} {search_text} {item_family} {item_module} {' '.join(item_tags)}"
            score = 0
            if query:
                if alias_text == query:
                    score += 100
                elif alias_text.startswith(query):
                    score += 40
                if query in search_text:
                    score += 20
            for term in terms:
                if term == alias_text:
                    score += 30
                elif term in alias_text:
                    score += 12
                if term in search_text:
                    score += 5
                if term in {item_family, item_module, *item_tags}:
                    score += 3
            if terms and score == 0:
                continue
            hit = dict(item)
            hit["score"] = score
            scored.append((score, alias, hit))
        scored.sort(key=lambda item: (-item[0], item[1]))
        hits = [item for _, _, item in scored[:limit]]
        result: dict[str, Any] = {
            "hits": hits,
            "total_count": len(scored),
            "returned_count": len(hits),
            "top_k": limit,
            "truncated": len(scored) > len(hits),
            "applied_filters": {
                key: value
                for key, value in {
                    "query": query,
                    "namespace": namespace,
                    "family": family,
                    "module_id": module_id,
                    "tags": sorted(tags) if tags else None,
                }.items()
                if value
            },
        }
        if include_facets:
            result["facets"] = _search_facets(item for _, _, item in scored)
            if result["truncated"]:
                result["usage_hint"] = "Narrow with namespace, module_id, family, or tags."
        return result

    @staticmethod
    def _read_generation_tool(generation: ToolRegistryGeneration, alias: str) -> dict[str, Any] | None:
        record = generation.record_for_alias(alias)
        if record is None:
            return None
        return {
            "alias": record.alias,
            "invocation_mode": record.execution.invocation_mode.value,
            "description": record.description,
            "example": dict(record.example) if record.example is not None else None,
            "input_schema": dict(record.input_schema),
            "output_schema": dict(record.output_schema),
        }

    @staticmethod
    def _complete_builtin(
        record: CompiledToolRecord,
        output: dict[str, Any],
        *,
        llm_text: str = "",
        affordances: list[ToolAffordance] | None = None,
        context_delivery: dict[str, Any] | None = None,
    ) -> ToolInvocationResult:
        try:
            if record.output_model is None:
                raise TypeError("internal built-in has no OutputModel")
            validated = validate_output(record.output_model, output).model_dump(mode="json", exclude_none=True)
        except (ValidationError, TypeError) as exc:
            outcome = EffectOutcome.NONE if record.execution.effect_kind is EffectKind.NONE else EffectOutcome.NOT_APPLIED
            return FailedResult(
                error_code="output_validation_failed",
                error=str(exc),
                effect=outcome,
                retry=derive_retry_directive(record.execution, outcome),
                llm_text=f"Built-in output failed validation for {record.alias}.",
                details={"output_schema": record.output_schema},
            )
        return CompleteResult(
            output=validated,
            effect=EffectOutcome.NONE if record.execution.effect_kind is EffectKind.NONE else EffectOutcome.APPLIED,
            llm_text=llm_text.strip() or json.dumps(validated, ensure_ascii=False, sort_keys=True),
            affordances=list(affordances or ()),
            context_delivery=(
                dict(context_delivery)
                if isinstance(context_delivery, dict)
                else None
            ),
        )

    def _call_record_sync(
        self,
        record: CompiledToolRecord,
        call: ToolCallIR,
        validated: BaseModel | dict[str, Any],
        turn_id: str | None,
        budget: ToolCallBudget | None,
        allow_tools: bool,
    ) -> Any:
        args = _invocation_args(validated)
        result = record.binding.callable(
            CapabilityCall(
                name=record.canonical_path,
                args=args,
                meta=self._invocation_meta(call, turn_id=turn_id, budget=budget, allow_tools=allow_tools),
            )
        )
        if inspect.isawaitable(result):
            raise RuntimeError(f"tool requires async execution: {record.alias}")
        return result

    async def _call_record_async(
        self,
        record: CompiledToolRecord,
        call: ToolCallIR,
        validated: BaseModel | dict[str, Any],
        turn_id: str | None,
        budget: ToolCallBudget | None,
        allow_tools: bool,
    ) -> Any:
        args = _invocation_args(validated)
        capability_call = CapabilityCall(
            name=record.canonical_path,
            args=args,
            meta=self._invocation_meta(call, turn_id=turn_id, budget=budget, allow_tools=allow_tools),
        )
        if record.binding.async_callable is not None:
            result = record.binding.async_callable(capability_call)
            return await result if inspect.isawaitable(result) else result
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self.sync_executor, lambda: record.binding.callable(capability_call))
        return await result if inspect.isawaitable(result) else result

    def _normalize_invocation_result(
        self,
        record: CompiledToolRecord,
        call: ToolCallIR,
        raw: Any,
        *,
        budget: ToolCallBudget | None,
        turn_id: str | None,
    ) -> ToolInvocationResult:
        if isinstance(raw, (CompleteResult, PagedResult, RejectedResult, FailedResult)):
            return raw
        receipt: EffectReceipt | None = None
        affordances: list[ToolAffordance] = []
        llm_text = ""
        context_delivery: dict[str, Any] | None = None
        if isinstance(raw, ToolHandlerResult):
            candidate = raw.output
            receipt = raw.effect_receipt
            affordances = list(raw.affordances)
            llm_text = raw.llm_text
        elif isinstance(raw, CapabilityResult) or all(
            hasattr(raw, attribute) for attribute in ("status", "text", "structured", "llm_text")
        ):
            llm_text = str(getattr(raw, "llm_text", "") or "")
            raw_status = getattr(raw, "status", RuntimeStatus.ERROR)
            raw_structured = getattr(raw, "structured", None)
            raw_text = str(getattr(raw, "text", "") or "")
            raw_receipt = getattr(raw, "effect_receipt", None)
            raw_delivery = getattr(raw, "context_delivery", None)
            if isinstance(raw_delivery, dict):
                context_delivery = dict(raw_delivery)
            if isinstance(raw_receipt, EffectReceipt):
                receipt = raw_receipt
            if raw_status != RuntimeStatus.OK:
                outcome = (
                    EffectOutcome.NONE
                    if record.execution.effect_kind is EffectKind.NONE
                    else receipt.outcome
                    if receipt is not None
                    else EffectOutcome.UNKNOWN
                )
                return FailedResult(
                    error_code=str((raw_structured or {}).get("error_code") or raw_status or "handler_failed"),
                    error=raw_text or llm_text,
                    effect=outcome,
                    retry=derive_retry_directive(record.execution, outcome),
                    llm_text=llm_text or raw_text,
                    details=dict(raw_structured or {}),
                )
            candidate = raw_structured if raw_structured is not None else {"text": raw_text}
            if record.is_mcp and isinstance(candidate, dict) and isinstance(candidate.get("raw_result"), dict):
                mcp_raw = dict(candidate["raw_result"])
                candidate = mcp_raw.get("structuredContent") if record.output_schema != McpToolOutput.model_json_schema(mode="validation") else {
                    "content": list(mcp_raw.get("content") or []),
                    "structured_content": mcp_raw.get("structuredContent"),
                    "is_error": bool(mcp_raw.get("isError")),
                }
                receipt = EffectReceipt(outcome=EffectOutcome.APPLIED, receipt={"mcp_response": True})
        else:
            candidate = raw

        if record.execution.effect_kind is EffectKind.NONE:
            outcome = EffectOutcome.NONE
        elif receipt is not None:
            outcome = receipt.outcome
        elif record.requires_effect_receipt:
            outcome = EffectOutcome.UNKNOWN
            return FailedResult(
                error_code="missing_effect_receipt",
                error=f"effectful handler for {record.alias} returned no effect receipt",
                effect=outcome,
                retry=derive_retry_directive(record.execution, outcome),
                llm_text=f"Effect outcome is unknown for {record.alias}; reconcile before retrying.",
            )
        else:
            outcome = EffectOutcome.APPLIED

        try:
            if record.is_mcp:
                Draft202012Validator(record.output_schema).validate(candidate)
                output: Any = candidate
            else:
                if record.output_model is None:
                    raise TypeError("internal tool has no OutputModel")
                if record.output_model is ProviderPayloadOutput:
                    candidate = (
                        candidate
                        if isinstance(candidate, dict)
                        and set(candidate) == {"payload"}
                        and isinstance(candidate.get("payload"), dict)
                        else {
                            "payload": (
                                dict(candidate)
                                if isinstance(candidate, dict)
                                else {"value": candidate}
                            )
                        }
                    )
                output_model = validate_output(record.output_model, candidate)
                output = output_model.model_dump(mode="json", exclude_none=True)
        except (ValidationError, JsonSchemaValidationError, TypeError) as exc:
            return FailedResult(
                error_code="output_validation_failed",
                error=str(exc),
                effect=outcome,
                retry=derive_retry_directive(record.execution, outcome),
                llm_text=f"Tool output failed validation for {record.alias}; effect={outcome.value}.",
                details={"output_schema": record.output_schema},
            )
        rendered = llm_text.strip() or json.dumps(output, ensure_ascii=False, sort_keys=True)
        paged = self._page_validated_output(
            record,
            call,
            output,
            rendered,
            outcome,
            budget,
            turn_id=turn_id,
            context_delivery=context_delivery,
        )
        if paged is not None:
            return paged
        return CompleteResult(
            output=output,
            effect=outcome,
            llm_text=rendered,
            affordances=affordances,
            context_delivery=context_delivery,
        )

    def _page_validated_output(
        self,
        record: CompiledToolRecord,
        call: ToolCallIR,
        output: Any,
        rendered: str,
        outcome: EffectOutcome,
        budget: ToolCallBudget | None,
        *,
        turn_id: str | None,
        context_delivery: dict[str, Any] | None,
    ) -> PagedResult | None:
        if budget is None or record.execution.paging is PagingMode.NEVER:
            return None
        char_limit = self._resolve_char_limit(budget)
        serialized = json.dumps(output, ensure_ascii=False, sort_keys=True)
        if char_limit is None or len(serialized) <= char_limit:
            return None
        result_ref = str(call.call_id or "").strip() or f"call_{uuid4().hex[:12]}"
        page_size = min(max(256, int(budget.preview_chars or 1000)), char_limit)
        handle = self.tool_result_pager.store(
            runtime_root=self.runtime_root,
            turn_id=str(turn_id or budget.artifact_bucket_id or result_ref),
            result_ref=result_ref,
            tool_name=record.alias,
            status=RuntimeStatus.OK,
            ok=True,
            rendered=rendered,
            page_size=page_size,
            output_json=serialized,
            origin={
                "alias": record.alias,
                "arguments": dict(call.args or {}),
                "invocation_mode": record.execution.invocation_mode.value,
                "search_text": record.search_text,
                "execution": record.execution.model_dump(mode="json"),
                "effect": outcome.value,
            },
            context_delivery=context_delivery,
        )
        page = self.tool_result_pager.read_page(
            result_ref,
            page=1,
            turn_id=turn_id,
        )
        page_text = page.content if page is not None else rendered[:page_size]
        affordances: list[ToolAffordance] = []
        if handle.page_count > 1:
            affordances.append(
                ToolAffordance(
                    tool="read_tool_result",
                    arguments={"result_ref": result_ref, "page": 2, "anchor": "head"},
                    reason="Read the exact next page of the validated complete output.",
                )
            )
        return PagedResult(
            result_handle={
                "result_ref": handle.result_ref,
                "page_size": handle.page_size,
                "original_size": handle.original_size,
                "page_count": handle.page_count,
                "created_user_turn": handle.created_user_turn,
                "expires_at_user_turn": handle.expires_at_user_turn,
            },
            page_text=page_text,
            effect=outcome,
            llm_text=page_text,
            affordances=affordances,
            context_delivery=(
                dict(page.context_delivery)
                if page is not None and page.context_delivery
                else None
            ),
        )

    @staticmethod
    def _rejected_error_result(exc: ToolRejectedError) -> RejectedResult:
        return rejection(
            exc.error_code,
            str(exc),
            retry=exc.retry,
            affordances=list(exc.affordances),
            details=dict(exc.details),
        )

    @staticmethod
    def _handler_exception_result(record: CompiledToolRecord, exc: Exception) -> FailedResult:
        receipt = getattr(exc, "effect_receipt", None)
        if record.execution.effect_kind is EffectKind.NONE:
            outcome = EffectOutcome.NONE
        elif isinstance(receipt, EffectReceipt):
            outcome = receipt.outcome
        else:
            outcome = EffectOutcome.UNKNOWN
        affordances = list(getattr(exc, "affordances", ()) or ())
        if not affordances:
            affordances = [
                ToolAffordance(
                    tool="read_tool",
                    arguments={"name": record.alias},
                    reason=(
                        "Review failure and retry semantics before recovering; reconcile the external effect first "
                        "when effect=unknown."
                    ),
                )
            ]
        return FailedResult(
            error_code=str(getattr(exc, "error_code", "handler_exception") or "handler_exception"),
            error=f"{exc.__class__.__name__}: {exc}",
            effect=outcome,
            retry=derive_retry_directive(record.execution, outcome),
            llm_text=f"Tool {record.alias} failed; effect={outcome.value}. {exc.__class__.__name__}: {exc}",
            affordances=affordances,
            details=dict(getattr(exc, "details", {}) or {}),
        )

    @staticmethod
    def _canonical_result_from_invocation(
        alias: str,
        call_id: str | None,
        result: ToolInvocationResult,
    ) -> ToolExecutionResult:
        rendered = ExecutionRuntime._render_invocation_for_llm(result)
        if isinstance(result, CompleteResult):
            structured = result.output if isinstance(result.output, dict) else {"output": result.output}
            return ToolExecutionResult(
                name=alias,
                ok=True,
                text=rendered,
                structured=structured,
                call_id=call_id,
                llm_text=rendered,
                status=RuntimeStatus.OK,
                invocation_result=result,
                context_delivery=(
                    dict(result.context_delivery)
                    if isinstance(result.context_delivery, dict)
                    else None
                ),
            )
        payload = result.model_dump(mode="json")
        return ToolExecutionResult(
            name=alias,
            ok=False if isinstance(result, (RejectedResult, FailedResult)) else True,
            text=rendered,
            structured=payload,
            call_id=call_id,
            llm_text=rendered,
            status=result.error_code if isinstance(result, (RejectedResult, FailedResult)) else "paged",
            invocation_result=result,
            context_delivery=(
                dict(result.context_delivery)
                if isinstance(result, PagedResult)
                and isinstance(result.context_delivery, dict)
                else None
            ),
        )

    @staticmethod
    def _render_invocation_for_llm(result: ToolInvocationResult) -> str:
        preserves_delivery_offsets = isinstance(
            getattr(result, "context_delivery", None),
            dict,
        )
        base = str(result.llm_text or "")
        if not preserves_delivery_offsets:
            base = base.strip()
        metadata: dict[str, Any] = {
            "kind": result.kind,
            "effect": result.effect.value,
        }
        if isinstance(result, (RejectedResult, FailedResult)):
            metadata.update(
                {
                    "error_code": result.error_code,
                    "retry": result.retry.value,
                }
            )
        if isinstance(result, PagedResult):
            metadata["result_handle"] = dict(result.result_handle)
        if result.affordances:
            metadata["affordances"] = [item.model_dump(mode="json") for item in result.affordances]
        if metadata == {"kind": "complete", "effect": EffectOutcome.NONE.value}:
            return base
        rendered_metadata = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        rendered = f"{base}\n\nTool result metadata: {rendered_metadata}"
        return rendered.rstrip() if preserves_delivery_offsets else rendered.strip()

    def execute_tool(
        self,
        call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
    ) -> ToolExecutionResult:
        captured = self._registry_generation
        invocation = self.invoke_direct_tool(
            call,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
            generation=captured,
        )
        return self._canonical_result_from_invocation(call.name, getattr(call, "call_id", None), invocation)

    async def execute_tool_async(
        self,
        call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
    ) -> ToolExecutionResult:
        captured = self._registry_generation
        invocation = await self.invoke_direct_tool_async(
            call,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
            generation=captured,
        )
        return self._canonical_result_from_invocation(call.name, getattr(call, "call_id", None), invocation)

    def _invocation_meta(
        self,
        call: ToolCallIR,
        *,
        turn_id: str | None,
        budget: ToolCallBudget | None,
        allow_tools: bool,
    ) -> dict[str, Any]:
        return {
            "turn_id": str(turn_id or ""),
            "tool_call": call,
            "budget": budget,
            "allow_tools": bool(allow_tools),
            "execution_runtime": self,
        }

    def _resolve_char_limit(self, budget: ToolCallBudget) -> int | None:
        candidates = [value for value in (budget.max_output_chars, budget.max_stdout_chars) if isinstance(value, int) and value > 0]
        if not candidates:
            return None
        return min(candidates)

    def call_registered(self, call: CapabilityCall) -> CapabilityResult:
        generation = self._registry_generation
        canonical_path = generation.capability_index.canonical_path_for(call.name)
        call = CapabilityCall(name=canonical_path, args=dict(call.args), meta=dict(call.meta))
        bound = self._resolve_binding(call, generation=generation)
        if isinstance(bound, CapabilityResult):
            return bound
        lifecycle_result = self._maybe_handle_lifecycle_action(bound.descriptor)
        if lifecycle_result is not None:
            return lifecycle_result
        result = bound.callable(call)
        if inspect.isawaitable(result):
            raise RuntimeError(f"capability requires async execution: {canonical_path}")
        return result

    async def call_registered_async(self, call: CapabilityCall) -> CapabilityResult:
        generation = self._registry_generation
        canonical_path = generation.capability_index.canonical_path_for(call.name)
        call = CapabilityCall(name=canonical_path, args=dict(call.args), meta=dict(call.meta))
        bound = self._resolve_binding(call, generation=generation)
        if isinstance(bound, CapabilityResult):
            return bound
        lifecycle_result = self._maybe_handle_lifecycle_action(bound.descriptor)
        if lifecycle_result is not None:
            return lifecycle_result
        if bound.async_callable is not None:
            result = bound.async_callable(call)
            return await result if inspect.isawaitable(result) else result
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self.sync_executor, lambda: bound.callable(call))
        return await result if inspect.isawaitable(result) else result

    def _resolve_binding(
        self,
        call: CapabilityCall,
        *,
        generation: ToolRegistryGeneration | None = None,
    ) -> BoundCapabilityAction | CapabilityResult:
        captured = generation or self._registry_generation
        singleton = captured.canonical_bindings.get(call.name, SINGLETON_TARGET)
        if singleton is not None:
            return singleton

        target_id = str(call.args.get("target_id") or SINGLETON_TARGET)
        bound = captured.canonical_bindings.get(call.name, target_id)
        if bound is not None:
            return bound
        matching = captured.capability_index.by_canonical.get(call.name, [])
        if matching and target_id == SINGLETON_TARGET:
            descriptors = [captured.capability_index.records[record_id] for record_id in matching]
            instance_targets = sorted(
                {
                    descriptor.target_id
                    for descriptor in descriptors
                    if descriptor.target_id and descriptor.target_id != SINGLETON_TARGET
                }
            )
            if instance_targets:
                return _target_id_required_result(canonical_path=call.name, available_target_ids=instance_targets)
        return CapabilityResult(
            status=RuntimeStatus.ERROR,
            text=f"unknown capability: {call.name}",
            llm_text=f"unknown capability: {call.name}",
        )

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
            effect_receipt=EffectReceipt(
                outcome=(
                    EffectOutcome.APPLIED
                    if status == RuntimeStatus.OK
                    else EffectOutcome.NOT_APPLIED
                ),
                receipt=payload,
            ),
        )

    def _resolve_descriptor(self, name: str, *, target_id: str = SINGLETON_TARGET) -> CapabilityDescriptor | CapabilityResult | None:
        candidates: list[CapabilityDescriptor] = []
        raw = str(name or "").strip()
        direct = self.compiled_capability_index.records.get(raw)
        if direct is not None:
            return direct
        canonical_path = self.resolve_capability_address(raw)
        if target_id != SINGLETON_TARGET:
            targeted_name = f"{raw}__{target_id}"
            targeted = self.compiled_capability_index.records.get(targeted_name)
            if targeted is not None:
                candidates.append(targeted)
        candidates.extend(
            self.compiled_capability_index.records[record_id]
            for record_id in self.compiled_capability_index.by_canonical.get(canonical_path, [])
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

    def resolve_capability_address(self, name: object) -> str:
        return self.compiled_capability_index.canonical_path_for(str(name or "").strip())

    def project_llm_text(self, value: object) -> str:
        generation = self.registry_generation
        return generation.project_llm_text(value)

    def project_llm_value(self, value: Any) -> Any:
        generation = self.registry_generation
        return generation.project_llm_value(value)

    def execute(self, call: CapabilityCall) -> CapabilityResult:
        try:
            result = self.call_registered(
                CapabilityCall(
                    name=call.name,
                    args=dict(call.args),
                    meta={
                        **dict(call.meta),
                        "direct_context_id": (
                            str(call.meta.get("direct_context_id") or "")
                            or f"runtime:{id(self)}"
                        ),
                    },
                )
            )
            return CapabilityResult(
                status=result.status,
                text=result.text,
                structured=result.structured,
                llm_text=getattr(result, "llm_text", ""),
                context_delivery=getattr(result, "context_delivery", None),
            )
        except Exception as exc:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text=f"capability execution failed: {exc.__class__.__name__}",
                structured={"error": str(exc), "capability": call.name},
                llm_text=f"capability execution failed: {exc.__class__.__name__}",
            )

    async def execute_async(self, call: CapabilityCall) -> CapabilityResult:
        try:
            result = await self.call_registered_async(
                CapabilityCall(
                    name=call.name,
                    args=dict(call.args),
                    meta={
                        **dict(call.meta),
                        "direct_context_id": (
                            str(call.meta.get("direct_context_id") or "")
                            or f"runtime:{id(self)}"
                        ),
                    },
                )
            )
            return CapabilityResult(
                status=result.status,
                text=result.text,
                structured=result.structured,
                llm_text=getattr(result, "llm_text", ""),
                context_delivery=getattr(result, "context_delivery", None),
            )
        except Exception as exc:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text=f"capability execution failed: {exc.__class__.__name__}",
                structured={"error": str(exc), "capability": call.name},
                llm_text=f"capability execution failed: {exc.__class__.__name__}",
            )

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


def _search_facets(records: Any) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {
        "namespaces": {},
        "modules": {},
        "families": {},
    }
    for record in records:
        for bucket, key, field_name in (
            ("namespaces", "namespace", "namespace"),
            ("modules", "module_id", "module_id"),
            ("families", "family", "family"),
        ):
            value = str(record.get(field_name) or "unknown")
            counts[bucket][value] = counts[bucket].get(value, 0) + 1
    return {
        "namespaces": [
            {"namespace": key, "count": count}
            for key, count in sorted(counts["namespaces"].items())
        ],
        "modules": [
            {"module_id": key, "count": count}
            for key, count in sorted(counts["modules"].items())
        ],
        "families": [
            {"family": key, "count": count}
            for key, count in sorted(counts["families"].items())
        ],
    }


def _capability_spec_payload(descriptor: CapabilityDescriptor) -> dict[str, Any]:
    canonical = descriptor.canonical_path or descriptor.name
    display = descriptor.display_name or descriptor.name
    call_names: list[str] = []
    for value in (canonical, descriptor.name, display, *descriptor.aliases):
        normalized = str(value or "").strip()
        if normalized and normalized not in call_names:
            call_names.append(normalized)
    return {
        "canonical_path": canonical,
        "name": descriptor.name,
        "display_name": display,
        "family": descriptor.family,
        "description": descriptor.description,
        "module_id": descriptor.module_id,
        "call_names": call_names,
        "aliases": list(descriptor.aliases),
        "input_schema": dict(
            descriptor.mcp_input_schema
            if descriptor.InputModel is None
            else descriptor.InputModel.model_json_schema(mode="validation")
        ),
        "output_schema": dict(
            descriptor.mcp_output_schema
            if descriptor.OutputModel is None
            else descriptor.OutputModel.model_json_schema(mode="validation")
        ),
        "source": descriptor.source,
        "target_kind": descriptor.target_kind,
        "target_id": descriptor.target_id,
        "metadata": dict(descriptor.metadata or {}),
    }
