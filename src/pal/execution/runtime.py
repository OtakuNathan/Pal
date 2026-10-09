from __future__ import annotations

from typing import cast
from pal.execution.turn_io_contracts import TURN_IO, TurnIOPort

import hashlib

from pal.execution.result_snapshots import ResultSnapshotStore, head_tail, render_snapshot_hint

from pal.shared.tool_protocol import ToolCallIR, ToolContextMessageIR

from pal.shared.tool_protocol import new_tool_call

import asyncio
import contextlib
import inspect
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    Idempotency,
    InvocationMode,
    McpToolOutput,
    PagingMode,
    RejectedResult,
    RetryDirective,
    RetryPolicy,
    ToolAffordance,
    ToolExecutionError,
    ToolHandlerResult,
    ToolInvocationResult,
    ToolRejectedError,
    derive_retry_directive,
    dump_input,
    dump_output,
    rejection,
    render_invalid_arguments,
    validate_output,
    validation_error_details,
)
from pal.shared.diagnostics import diagnostic_text, diagnostic_value, diagnostic_summary, exception_report, exception_summary
from pal.execution.tool_presentation import render_tool_definition, render_tool_search
from pal.shared.result_rendering import render_structured_for_llm
from pal.execution.tool_registry import (
    CompiledToolRecord,
    ToolRegistryGeneration,
    compile_registry_generation,
)
from pal.execution.result_guidance import (
    action_key,
    normalize_affordances,
    resolve_failure_guidance,
)
from pal.shared import ToolExecutionResult
from pal.plugins.l3.registry import L3PluginRegistry
from pal.plugins.l3.stubs import NullL3Plugin
from pal.plugins.lifecycle import WriterPreferredRWGate
from pal.execution.logical_sessions import LogicalExecutionSessions
from pal.execution.session_state import DEFAULT_RESULT_RETENTION_USER_TURNS
from pal.execution.session_state import (
    FileDeliveryManifest,
    InMemoryLogicalExecutionState,
    LogicalExecutionContext,
    LogicalExecutionStateBackend,
)
from pal.shared import (
    BoundCapabilityAction,
    MountedSubtreeHandle,
    RuntimeStatus,
    SINGLETON_TARGET,
)
from pal.execution.discovery_terms import tool_search_terms

if TYPE_CHECKING:
    from pal.core.module_registry import ModuleHandle


# Diagnostic previews are independent of the caller's normal-output budget.
# Complete diagnostics remain in snapshots; status and recovery are outside it.
_DIAGNOSTIC_PREVIEW_CHARS = 1600
_LOGGER = logging.getLogger(__name__)

# A leading structured fact block larger than this is not treated as a
# minimum envelope (it would dwarf the budget it claims to be necessary for).
_MAX_FACT_BLOCK_CHARS = 1_200


def _leading_fact_block(text: str) -> str | None:
    """Return the leading JSON object of a facts-first result body, if any."""

    import json

    stripped = str(text or "").lstrip()
    try:
        value, end = json.JSONDecoder().raw_decode(stripped)
    except ValueError:
        return None
    if not isinstance(value, dict) or end > _MAX_FACT_BLOCK_CHARS:
        return None
    return stripped[:end]


def _is_plugin_lifecycle_tool(name: object) -> bool:
    normalized = str(name or "").strip()
    aliases = {
        "attach_plugin",
        "reload_plugin",
        "detach_plugin",
        "enable_plugin",
        "disable_plugin",
        "rescan_plugins",
        "rescan_and_attach_first_party_plugins",
    }
    if normalized in aliases:
        return True
    return normalized in {
        f"op_plugin_mgmt_{action}" for action in (
            "attach", "reattach", "detach", "enable", "disable", "rescan", "rescan_and_attach_new_first_party"
        )
    }


def _is_package_job_tool(name: object) -> bool:
    # These resident handlers only queue/wait/read jobs. The background package
    # owner holds the write fence for activation; a waiting read fence would
    # prevent that worker from completing.
    return str(name or "").strip() in {
        "install_package", "prepare_package", "inspect_package_status", "uninstall_plugin",
        "op_plugin_package_install", "op_plugin_package_prepare",
        "intro_module_plugins_status", "op_plugin_mgmt_uninstall",
    }


def _invocation_args(
    validated: BaseModel | dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(validated, BaseModel):
        return dict(validated)
    return dump_input(validated)


def _failure_effect_outcome(record: CompiledToolRecord, receipt: EffectReceipt | None) -> EffectOutcome:
    if receipt is not None:
        return receipt.outcome
    if record.execution.effect_kind is EffectKind.NONE:
        return EffectOutcome.NONE
    # Read-labelled tools can still change session state, e.g. navigation or
    # memory promotion. Only the handler can confirm that no effect occurred.
    return EffectOutcome.UNKNOWN


def _with_failure_diagnostics(text: str, structured: dict[str, Any], *, error: str = "") -> str:
    """Keep handler diagnostics visible even when its summary omits them."""
    error = diagnostic_text(error, limit=None)
    if error and error not in text:
        text += "\nReported error: " + error
    missing = {}
    for key, value in diagnostic_value(structured).items():
        if isinstance(value, str) and (not value or value in text):
            continue
        serialized_field = render_structured_for_llm({key: value})[1:-1]
        if serialized_field not in text:
            missing[key] = value
    if missing:
        text += "\nFailure details: " + render_structured_for_llm(missing)
    return text


@dataclass
class ExecutionRuntime(ExecutionRuntimePort):
    activity_decorator: Any | None = field(default=None, kw_only=True)
    provider_registry: dict[str, Any] = field(default_factory=dict)
    l3_plugin_registry: L3PluginRegistry = field(default_factory=L3PluginRegistry)
    runtime_root: Path | None = None
    logical_state: LogicalExecutionStateBackend = field(
        default_factory=InMemoryLogicalExecutionState
    )
    execution_sessions: LogicalExecutionSessions = field(default_factory=LogicalExecutionSessions)
    result_snapshots: ResultSnapshotStore | None = None
    lifecycle_controller: Any | None = None
    lifecycle_gate: WriterPreferredRWGate = field(default_factory=WriterPreferredRWGate)
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

    def adopt_host_state(self, previous: ExecutionRuntime) -> None:
        """Transfer framework-owned session state at a fenced implementation swap.

        Plugin-owned fields stay with their implementation. Keep this explicit so
        adding an implementation field cannot silently make it transferable.
        """
        self.activity_decorator = previous.activity_decorator
        self.provider_registry = previous.provider_registry
        self.l3_plugin_registry = previous.l3_plugin_registry
        self.runtime_root = previous.runtime_root
        self.logical_state = previous.logical_state
        self.execution_sessions = previous.execution_sessions
        self.result_snapshots = previous.result_snapshots
        self.lifecycle_controller = previous.lifecycle_controller
        self.lifecycle_gate = previous.lifecycle_gate
        self.sync_executor_max_workers = previous.sync_executor_max_workers
        self.sync_executor = previous.sync_executor
        self._interrupt_handles = previous._interrupt_handles
        self._interrupt_tasks = previous._interrupt_tasks
        self._interrupt_state_lock = previous._interrupt_state_lock
        self._registry_lock = previous._registry_lock
        self._registry_generation = previous._registry_generation

    def __post_init__(self) -> None:
        self.execution_sessions.state_backend = self.logical_state
        if self.result_snapshots is None:
            self.result_snapshots = ResultSnapshotStore(self.runtime_root)
        default_l3 = NullL3Plugin()
        self.provider_registry.setdefault(default_l3.provider_id, default_l3)
        if self.l3_plugin_registry.get(default_l3.provider_id) is None:
            self.l3_plugin_registry.register(default_l3)
        if self.sync_executor is None:
            self.sync_executor = ThreadPoolExecutor(
                max_workers=self.sync_executor_max_workers,
                thread_name_prefix="pal-exec",
            )

    def build_introspection_provider(self):
        from .capabilities import ExecutionIntrospectionProvider
        return ExecutionIntrospectionProvider(runtime=self)

    def build_runtime_state_port(self):
        from .runtime_state import ExecutionRuntimeStatePort
        return ExecutionRuntimeStatePort(self)

    def project_execution_view(self, view):
        return view

    def role_capabilities(self, allowed):
        return allowed

    def project_role_descriptor(self, descriptor):
        return descriptor

    def create_role_session_driver(self):
        return None

    def prepare_model_context(self, memory, continuation, *, context_view=None):
        """Record file-result visibility for this request, not the whole lifetime."""
        result_ids = (
            (call_id for turn in context_view.turns.values() for call_id in turn.tool_results)
            if context_view is not None else ()
        )
        self.execution_sessions.set_visible_results(continuation.turn_id, result_ids)

    def model_response_received(self, continuation):
        """Allow an optional execution owner to refresh outside request assembly."""

    def observe_tool_delivery(self, call, result):
        """Record coverage proven by a durably committed tool result."""

    def stagnation_payload(self, call, result):
        return {"ok": result.ok, "text": result.text, "structured": result.structured}

    async def close_role_work(self):
        pass

    async def complete_evidence(self, call, result, **kwargs):
        return result

    def execution_diagnostics(self):
        return {"backend": "python", "sessions": 0}

    async def shutdown_async(self):
        self.shutdown()

    async def prepare_shutdown_async(self):
        pass

    def shutdown(self) -> None:
        self.result_snapshots.discard_pending()
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

    @property
    def turn_io(self) -> TurnIOPort | None:
        return cast(TurnIOPort | None, self.provider_registry.get(TURN_IO.name))

    def register_provider_ref(self, provider_id: str, provider: Any) -> None:
        if provider_id == TURN_IO.name:
            TURN_IO.validate(provider)
        self.provider_registry[provider_id] = provider

    def unregister_provider_ref(self, provider_id: str) -> None:
        self.provider_registry.pop(provider_id, None)

    def begin_tool_result_turn(
        self,
        *,
        turn_id: str,
        scope_key: str = "",
        retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS,
        input_id: str = "",
    ) -> LogicalExecutionContext:
        return self.execution_sessions.begin_turn(
            runtime_root=self.runtime_root,
            turn_id=turn_id,
            scope_key=scope_key,
            retention_user_turns=retention_user_turns,
            input_id=input_id,
        )

    def advance_tool_result_clock(
        self,
        *,
        turn_id: str,
        clock_id: str,
        retention_steps: int | None = None,
    ) -> LogicalExecutionContext:
        """Advance the logical input clock; output files do not expire by this clock.

        Resident Pal advances the same backend with semantic user inputs.
        Autonomous runtimes such as Bunshin may instead advance it per tool
        call without changing L1 or coroutine state.
        """

        context = self.logical_context_for_turn(turn_id)
        return self.execution_sessions.begin_turn(
            runtime_root=self.runtime_root,
            turn_id=turn_id,
            scope_key=context.execution_lifetime_id,
            retention_user_turns=retention_steps,
            input_id=clock_id,
        )

    def logical_context_for_turn(self, turn_id: str | None) -> LogicalExecutionContext:
        return self.execution_sessions.context_for_turn(turn_id)

    def retire_tool_results(
        self,
        *,
        turn_id: str | None,
        result_ids: tuple[str, ...],
        execution_lifetime_id: str = "",
    ) -> tuple[str, ...]:
        """Retire file-read authority; L1 ownership separately retires output files."""

        normalized = tuple(
            result_id
            for result_id in (str(item or "").strip() for item in result_ids)
            if result_id
        )
        if not normalized:
            return ()
        lifetime = str(execution_lifetime_id or "").strip()
        if lifetime:
            return self.logical_state.retire_results(
                execution_lifetime_id=lifetime,
                result_ids=normalized,
            )
        context = self.logical_context_for_turn(turn_id)
        return self.logical_state.retire_results(
            execution_lifetime_id=context.execution_lifetime_id,
            result_ids=normalized,
        )

    def commit_tool_delivery(
        self,
        *,
        turn_id: str | None,
        context_delivery: dict[str, Any] | None,
        result_id: str = "",
    ) -> LogicalExecutionContext:
        """Commit a tool delivery after its result has entered L1."""

        context = self.logical_context_for_turn(turn_id)
        manifest = FileDeliveryManifest.from_dict(context_delivery)
        if manifest is None:
            return context
        delivery = manifest.to_dict()
        delivery["result_id"] = str(result_id or manifest.replay_result_ref)
        return self.logical_state.record_delivery(
            execution_lifetime_id=context.execution_lifetime_id,
            delivery=delivery,
        )

    def discard_uncommitted_tool_delivery(
        self,
        *,
        turn_id: str,
        result_ref: str,
    ) -> None:
        context = self.logical_context_for_turn(turn_id)
        self.result_snapshots.finish_delivery(lifetime=context.execution_lifetime_id, call_id=result_ref)

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
            "family": record.family,
            "purpose": record.guidance.purpose,
            "module": record.module_id,
            "description": record.compiled_description,
            "search_text": record.search_document,
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
        result = self._invoke_tool_record_unfinalized_sync(
            generation,
            call,
            invocation_mode=invocation_mode,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
        )
        return self._finalize_invocation_result(generation, call, result, budget=budget, turn_id=turn_id)

    def _invoke_tool_record_unfinalized_sync(
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
            # Builtin results flow to the invoke-level finalizer together
            # with every other exit; call_tool recursion finalizes once
            # inside its own wrapper and is returned idempotently here.
            return special
        if not allow_tools:
            return rejection(
                "finalization_only",
                "tool execution disabled in finalization mode",
                retry=RetryDirective.DO_NOT_RETRY,
            )
        binding = self._resolve_record_binding(generation, record, validated)
        if isinstance(binding, RejectedResult):
            return binding
        try:
            gate = self._invocation_gate(call.name)
            with gate:
                raw = self._call_record_sync(
                    record, binding, call, validated, turn_id, budget, allow_tools
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
        result = await self._invoke_tool_record_unfinalized_async(
            generation,
            call,
            invocation_mode=invocation_mode,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
        )
        return self._finalize_invocation_result(generation, call, result, budget=budget, turn_id=turn_id)

    async def _invoke_tool_record_unfinalized_async(
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
            # Builtin results flow to the invoke-level finalizer together
            # with every other exit; call_tool recursion finalizes once
            # inside its own wrapper and is returned idempotently here.
            return special
        if not allow_tools:
            return rejection(
                "finalization_only",
                "tool execution disabled in finalization mode",
                retry=RetryDirective.DO_NOT_RETRY,
            )
        binding = self._resolve_record_binding(generation, record, validated)
        if isinstance(binding, RejectedResult):
            return binding
        try:
            gate = self._invocation_gate(call.name, asynchronous=True)
            async with gate:
                raw = await self._call_record_async(
                    record, binding, call, validated, turn_id, budget, allow_tools
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
                validation_error_details(exc)
                if isinstance(exc, ValidationError)
                else {"validation_error": str(exc)}
            )
            return rejection(
                "invalid_arguments",
                render_invalid_arguments(record.alias, exc, record.input_schema),
                retry=RetryDirective.CORRECT_INPUT,
                details=details,
            )

    @staticmethod
    def _resolve_record_binding(
        generation: ToolRegistryGeneration,
        record: CompiledToolRecord,
        validated: BaseModel | dict[str, Any],
    ) -> BoundCapabilityAction | RejectedResult:
        target_argument = str(record.binding.descriptor.metadata.get("target_argument") or "")
        if not target_argument:
            return record.binding
        args = _invocation_args(validated)
        target_name = str(args.get(target_argument) or "").strip()
        binding = generation.canonical_bindings.get(record.canonical_path, target_name)
        if binding is not None:
            return binding
        available_names = sorted(
            {
                candidate_target
                for candidate_path, candidate_target in generation.canonical_bindings.actions
                if candidate_path == record.canonical_path and candidate_target != SINGLETON_TARGET
            }
        )
        discovery_alias = _target_discovery_alias(record.binding.descriptor)
        discovery_record = generation.record_for_alias(discovery_alias) if discovery_alias else None
        affordances: list[ToolAffordance] = []
        if discovery_record is not None:
            if discovery_record.execution.invocation_mode is InvocationMode.DIRECT:
                affordances.append(
                    ToolAffordance(
                        tool=discovery_alias,
                        arguments={},
                        reason=f"List valid {record.binding.descriptor.target_kind} names before retrying.",
                    )
                )
            else:
                affordances.append(
                    ToolAffordance(
                        tool="call_tool",
                        arguments={"name": discovery_alias, "args": {}},
                        reason=f"List valid {record.binding.descriptor.target_kind} names before retrying.",
                    )
                )
        return rejection(
            "unknown_target",
            (f"unknown {record.binding.descriptor.target_kind} name for {record.alias}: {target_name!r}. "
             f"Valid {target_argument} values: {diagnostic_text(', '.join(available_names[:20]) or '(none)')}"
             + ("; additional names available through discovery." if len(available_names) > 20 else "")),
            retry=RetryDirective.CORRECT_INPUT,
            affordances=affordances,
            details={"argument": target_argument, "available_names": available_names},
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
            payload = self._search_generation(generation, args)
            return self._complete_builtin(record, payload, llm_text=render_tool_search(generation, payload))
        if record.alias == "read_tool":
            payload = self._read_generation_tool(generation, str(args.get("name") or ""))
            if payload is None:
                return rejection(
                    "unknown_tool",
                    f"unknown tool alias: {args.get('name')}",
                    affordances=[ToolAffordance(tool="search_tools", arguments={"query": args.get("name") or ""}, reason="Search current aliases.")],
                )
            return self._complete_builtin(record, payload, llm_text=render_tool_definition(payload, view=args.get("view", "input")))
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
            payload = self._search_generation(generation, args)
            return self._complete_builtin(record, payload, llm_text=render_tool_search(generation, payload))
        if record.alias == "read_tool":
            payload = self._read_generation_tool(generation, str(args.get("name") or ""))
            if payload is None:
                return rejection(
                    "unknown_tool",
                    f"unknown tool alias: {args.get('name')}",
                    affordances=[ToolAffordance(tool="search_tools", arguments={"query": args.get("name") or ""}, reason="Search current aliases.")],
                )
            return self._complete_builtin(record, payload, llm_text=render_tool_definition(payload, view=args.get("view", "input")))
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
        return None

    @staticmethod
    def _search_generation(generation: ToolRegistryGeneration, args: dict[str, Any]) -> dict[str, Any]:
        raw_query = str(args.get("query") or "").strip()
        query = raw_query.lower()
        namespace = str(args.get("namespace") or "").strip().lower()
        namespace = {"inspect": "introspection", "action": "operation"}.get(namespace, namespace)
        family = str(args.get("family") or "").strip().lower()
        module_id = str(args.get("module_name") or args.get("module_id") or "").strip().lower()
        tags = {
            str(item).strip().lower()
            for item in list(args.get("tags") or ())
            if str(item).strip()
        }
        include_facets = bool(args.get("facets", False))
        explicit_limit = args.get("top_k") is not None or args.get("limit") is not None
        try:
            limit = max(1, int(args.get("top_k") or args.get("limit") or 3))
        except (TypeError, ValueError):
            limit = 3
        terms = set(tool_search_terms(query))
        score_base = max(6, len(terms) + 1)
        scored = []
        query_matches: list[dict[str, Any]] = []
        for alias, item in generation.search_records.items():
            item_namespace = str(item.get("namespace") or "").lower()
            item_family = str(item.get("family") or "").lower()
            item_module = str(item.get("module_id") or "").lower()
            item_tags = {str(tag).lower() for tag in item.get("tags", ())}
            alias_text = alias.lower()
            alias_terms = set(tool_search_terms(alias_text))
            object_terms = set(item.get("search_terms", item.get("search_objects", ())))
            object_matches = terms & object_terms
            alias_matches = terms & alias_terms
            matched_terms = alias_matches | object_matches
            coverage = len(matched_terms)
            # Only aliases and declared discovery vocabulary are positive evidence.
            # Prose can contain negations or mention unrelated tools.
            if raw_query and alias == raw_query:
                tier = 6
            elif query and alias_text == query:
                tier = 5
            elif terms and (terms == alias_terms or (
                    terms - object_terms == alias_terms - object_terms
                    and terms & object_terms and alias_terms & object_terms)):
                tier = 4
            elif terms and alias_matches | object_matches == terms:
                tier = 3
            else:
                tier = 0
            if query and not terms and alias_text != query:
                continue
            if terms and not tier:
                continue
            query_matches.append(item)
            if ((namespace and item_namespace != namespace)
                    or (family and item_family != family)
                    or (module_id and item_module != module_id)
                    or (tags and not tags.issubset(item_tags))):
                continue
            # This vocabulary belongs to the harness, not the model contract.
            hit = {key: value for key, value in item.items() if key not in {"search_objects", "search_terms"}}
            # All query words must match. Prefer exact aliases, then equivalent
            # object forms, then aliases containing additional words.
            rank = (max(0, tier - 4), coverage, tier)
            score = 0
            for component in rank:
                score = score * score_base + component
            hit["score"] = score
            scored.append((rank, alias, hit))
        scored.sort(key=lambda row: (-row[-1]["score"], row[1]))
        candidates = scored
        if not explicit_limit and scored and scored[0][0][2] >= 2:
            # A precise match should not be padded with weaker neighbours.
            # An explicit limit allows broader discovery when requested.
            candidates = [row for row in scored if row[0][:3] == scored[0][0][:3]]
        hits = [row[-1] for row in candidates[:limit]]
        result: dict[str, Any] = {
            "hits": hits,
            "total_count": len(scored),
            "returned_count": len(hits),
            "top_k": limit,
            "truncated": len(candidates) > len(hits),
            "omitted_weaker_count": len(scored) - len(candidates),
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
            result["facets"] = _search_facets(row[-1] for row in scored)
            if result["truncated"]:
                result["usage_hint"] = "Narrow with namespace, module_name, family, or tags."
        if not scored and query_matches:
            result["filter_suggestions"] = _search_facets(query_matches)
            result["usage_hint"] = (
                "The query matches tools, but the supplied filters exclude them. "
                "Remove or correct filters using filter_suggestions; family is not module_name."
            )
        elif not scored:
            result["usage_hint"] = "No matching tools. All query words must match an alias or declared discovery terms, including selected operation enums. Shorten to the object/domain (e.g. 'browser tabs'), omit unknown filters, then use read_tool to inspect available operations."
        return result

    @staticmethod
    def _read_generation_tool(generation: ToolRegistryGeneration, alias: str) -> dict[str, Any] | None:
        record = generation.record_for_alias(alias)
        if record is None:
            return None
        return {
            "alias": record.alias,
            "invocation_mode": record.execution.invocation_mode.value,
            "description": record.compiled_description,
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
            validated = dump_output(validate_output(record.output_model, output))
        except (ValidationError, TypeError) as exc:
            outcome = EffectOutcome.NONE if record.execution.effect_kind is EffectKind.NONE else EffectOutcome.NOT_APPLIED
            diagnostic = exception_report(exc)
            return FailedResult(
                error_code="output_validation_failed",
                error=exception_summary(exc),
                effect=outcome,
                retry=derive_retry_directive(record.execution, outcome),
                llm_text=(f"Built-in output failed validation for {record.alias}.\n"
                          f"Validation error:\n{diagnostic}\n"
                          "Use read_tool with view=output to inspect the output contract.\n"
                          "Unvalidated tool result:\n" + render_structured_for_llm({
                              "output": diagnostic_value(output), "llm_text": diagnostic_text(llm_text, limit=None),
                          })),
                affordances=[ToolAffordance(tool="read_tool", arguments={"name": record.alias, "view": "output"},
                                           reason="Inspect the output contract to repair the provider; do not replay a mutation.")],
                recovery_hint="Repair the tool/provider output contract, not the task arguments. Do not repeat side effects to recover output.",
                details={"output_schema": record.output_schema, "raw_output": output, "raw_llm_text": llm_text},
            )
        return CompleteResult(
            output=validated,
            effect=EffectOutcome.NONE if record.execution.effect_kind is EffectKind.NONE else EffectOutcome.APPLIED,
            llm_text=llm_text or render_structured_for_llm(validated),
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
        binding: BoundCapabilityAction,
        call: ToolCallIR,
        validated: BaseModel | dict[str, Any],
        turn_id: str | None,
        budget: ToolCallBudget | None,
        allow_tools: bool,
    ) -> Any:
        args = _invocation_args(validated)
        result = binding.callable(
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
        binding: BoundCapabilityAction,
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
        if binding.async_callable is not None:
            result = binding.async_callable(capability_call)
            return await result if inspect.isawaitable(result) else result
        result = await self._call_sync_handler_async(binding, capability_call)
        return await result if inspect.isawaitable(result) else result

    async def _call_sync_handler_async(
        self, binding: BoundCapabilityAction, call: CapabilityCall,
    ) -> Any:
        """Keep the caller's lifecycle fence until its worker actually exits."""
        cancelled = threading.Event()

        def invoke() -> Any:
            # Cancellation must still prevent a queued call from starting.
            if not cancelled.is_set():
                return binding.callable(call)
            return None

        worker = asyncio.get_running_loop().run_in_executor(self.sync_executor, invoke)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            # Cancelling the waiter cannot stop an already-running thread.
            # Drain it under the outer lifecycle fence, including subsequent
            # cancellation requests, then propagate the original cancellation.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not worker.cancelled():
                try:
                    result = worker.result()
                except BaseException as exc:
                    _LOGGER.warning(
                        "Synchronous capability %s failed while cancellation was pending:\n%s",
                        call.name, exception_report(exc),
                    )
                else:
                    if inspect.iscoroutine(result):
                        result.close()
            raise

    def _normalize_invocation_result(self, record, call, raw, *, budget, turn_id):
        # Normalization only: guidance resolution and the final model-text
        # budget happen once at the invoke-level finalizer, after every
        # override (including native appends) has contributed its fields.
        result = self._normalize_invocation_result_inner(record, call, raw, budget=budget, turn_id=turn_id)
        if not result.snapshot_refs and isinstance(raw, CapabilityResult) and raw.snapshot_refs:
            result = result.model_copy(update={"snapshot_refs": tuple(raw.snapshot_refs)})
        return result

    def deliver_invocation_result(
        self,
        record: CompiledToolRecord,
        call: ToolCallIR,
        raw: Any,
        *,
        budget: ToolCallBudget | None,
        turn_id: str | None,
    ) -> ToolInvocationResult:
        """Normalize and finalize one result for direct delivery paths.

        Background event and observation deliveries that bypass the invoke
        entry points still receive the same guidance resolution and final
        model-text budget as ordinary tool results (§9.2).
        """
        result = self._normalize_invocation_result(record, call, raw, budget=budget, turn_id=turn_id)
        return self._finalize_invocation_result(
            self._registry_generation, call, result, budget=budget, turn_id=turn_id
        )

    def _finalize_invocation_result(
        self,
        generation: ToolRegistryGeneration,
        call: ToolCallIR,
        result: ToolInvocationResult,
        *,
        budget: ToolCallBudget | None,
        turn_id: str | None,
    ) -> ToolInvocationResult:
        """Single authoritative exit for one logical tool result (§9.2-§9.3).

        Candidate guidance is resolved against the captured generation. Normal
        output is bounded separately from error presentation and metadata.
        Idempotent: an already-final result whose budgeted portion fits
        passes through unchanged, so nested
        wrappers (call_tool recursion, native overrides) never double-capture
        snapshots or stack fallbacks.
        """
        try:
            result = self._resolve_result_guidance(generation, result)
            if (isinstance(result, (FailedResult, RejectedResult))
                    and result.error_code != "invalid_arguments"
                    and not result.affordances and not result.recovery_hint):
                record = generation.record_for_alias(call.name)
                if record is not None and record.guidance.failure_next_steps.strip():
                    result = result.model_copy(update={"recovery_hint": record.guidance.failure_next_steps.strip()})
        except Exception as exc:
            # Guidance handling must never turn a delivered operation into a
            # new failure. Keep its recovery constraints and report this
            # additional fault without offering unvalidated actions.
            _LOGGER.warning("Unable to resolve tool result guidance", exc_info=True)
            updates = {
                "affordances": [],
                "llm_text": result.llm_text + "\nRecovery suggestions could not be validated; "
                    "operation status is unchanged:\n" + exception_report(exc),
            }
            if isinstance(result, (FailedResult, RejectedResult)):
                updates["error"] = result.error + "\nRecovery guidance also failed: " + exception_summary(exc)
            else:
                updates.pop("llm_text")
                updates["output_error"] = (
                    (result.output_error + "\n" if result.output_error else "")
                    + "Recovery suggestions could not be validated; operation status is unchanged:\n"
                    + exception_report(exc)
                )
            result = result.model_copy(update=updates)
        if isinstance(result, (FailedResult, RejectedResult)):
            # Redact the final failure projection, including provider output
            # rejected by validation, before any full diagnostic is retained
            # in a snapshot. Keep host evidence, executable arguments, and
            # successful business output unchanged.
            result = result.model_copy(update={
                "error": diagnostic_text(result.error, limit=None),
                "llm_text": diagnostic_text(result.llm_text, limit=None),
                "recovery_hint": diagnostic_text(result.recovery_hint, limit=None),
                "affordances": [action.model_copy(update={
                    "reason": diagnostic_text(action.reason, limit=None),
                }) for action in result.affordances],
            })
        if isinstance(result, (FailedResult, RejectedResult)):
            return self._present_failure_diagnostic(result, call, turn_id=turn_id)
        result = self._present_delivery_diagnostic(result, call, turn_id=turn_id)
        return self._budget_invocation_result(result, call, budget=budget, turn_id=turn_id)

    def _present_delivery_diagnostic(self, result: CompleteResult, call, *, turn_id):
        """Expose secondary delivery errors without rewriting successful output."""
        if not result.output_error:
            return result
        full = diagnostic_text(result.output_error, limit=None)
        if any(ref.coverage == "complete delivery diagnostic" and full.endswith(render_snapshot_hint(ref))
               for ref in result.snapshot_refs):
            return result
        summary = diagnostic_summary(full)
        if summary == full and len(full) <= _DIAGNOSTIC_PREVIEW_CHARS:
            return result.model_copy(update={"output_error": full})
        if len(summary) > _DIAGNOSTIC_PREVIEW_CHARS:
            summary, _ = head_tail(summary, _DIAGNOSTIC_PREVIEW_CHARS)
        try:
            lifetime = self.logical_context_for_turn(turn_id or call.call_id).execution_lifetime_id
            ref = self.result_snapshots.capture(full, call_id=call.call_id,
                lifetime=lifetime, coverage="complete delivery diagnostic")
        except Exception as exc:
            return result.model_copy(update={"output_error": full +
                "\nFull diagnostic could not be saved: " + exception_summary(exc)})
        return result.model_copy(update={
            "output_error": summary + "\n\nFull diagnostic:\n" + render_snapshot_hint(ref),
            "snapshot_refs": tuple(dict.fromkeys((*result.snapshot_refs, ref))),
        })

    def _present_failure_diagnostic(self, result, call, *, turn_id):
        """Keep actionable errors outside the business-output budget.

        Stack traces and large provider responses are evidence, not a useful
        default error message. Retain them before presenting a concise view.
        """
        text = result.llm_text
        if any(ref.coverage == "complete failure diagnostic" and text.endswith(render_snapshot_hint(ref))
               for ref in result.snapshot_refs):
            return result
        if len(text) <= _DIAGNOSTIC_PREVIEW_CHARS and "Traceback (most recent call last):" not in text:
            return result
        summary = diagnostic_summary(result.error or text)
        heading = text.split("\n", 1)[0]
        if heading and "Traceback (most recent call last):" not in heading and heading not in summary:
            summary = heading + "\n" + summary
        if result.details:
            safe_details = diagnostic_value(result.details, summarize=True)
            details = "\nFailure details: " + render_structured_for_llm(safe_details)
            if len(summary) + len(details) <= _DIAGNOSTIC_PREVIEW_CHARS:
                summary += details
        if len(summary) > _DIAGNOSTIC_PREVIEW_CHARS:
            summary, _ = head_tail(summary, _DIAGNOSTIC_PREVIEW_CHARS)
        # Include recovery in the saved diagnostic as well as live metadata.
        full = text + self._rendered_guidance_tail(result)
        try:
            lifetime = self.logical_context_for_turn(turn_id or call.call_id).execution_lifetime_id
            ref = self.result_snapshots.capture(full, call_id=call.call_id,
                lifetime=lifetime, coverage="complete failure diagnostic")
        except Exception as exc:
            # Never shorten evidence without a retained full copy.
            return result.model_copy(update={"llm_text": text +
                "\nFull diagnostic could not be saved: " + exception_summary(exc) +
                "\nThe complete diagnostic is shown above; operation status is unchanged."})
        return result.model_copy(update={
            "llm_text": summary + "\n\nFull diagnostic:\n" + render_snapshot_hint(ref),
            "snapshot_refs": tuple(dict.fromkeys((*result.snapshot_refs, ref))),
        })

    def _resolve_result_guidance(
        self,
        generation: ToolRegistryGeneration,
        result: ToolInvocationResult,
    ) -> ToolInvocationResult:
        """Validate and dedup suggested actions against the captured view (§8)."""
        resolved: list[ToolAffordance] = []
        diagnostics: list[str] = []
        for candidate in normalize_affordances(result.affordances or (), limit=None):
            try:
                key = action_key(candidate)
                record = generation.record_for_alias(key.alias)
                if record is None or key.alias == "call_tool":
                    continue
                arguments = json.loads(key.arguments_json)
                validated = self._validate_invocation_input(record, arguments)
                if isinstance(validated, RejectedResult):
                    continue
                if isinstance(self._resolve_record_binding(generation, record, validated), RejectedResult):
                    continue
                if key.alias == "read_tool" and generation.record_for_alias(arguments["name"]) is None:
                    continue
                if key.alias in generation.direct_aliases:
                    action = ToolAffordance(tool=key.alias, arguments=arguments, reason=candidate.reason)
                else:
                    wrapper = generation.direct_aliases.get("call_tool")
                    if wrapper is None:
                        continue
                    arguments = {"name": key.alias, "args": arguments}
                    if isinstance(self._validate_invocation_input(wrapper, arguments), RejectedResult):
                        continue
                    action = ToolAffordance(tool="call_tool", arguments=arguments, reason=candidate.reason)
                resolved.append(action)
            except Exception as exc:
                _LOGGER.warning("Dropping invalid tool result affordance", exc_info=True)
                diagnostics.append(exception_report(exc))
        resolved = normalize_affordances(resolved)
        if resolved == list(result.affordances or ()) and not diagnostics:
            return result
        updates = {"affordances": resolved}
        if diagnostics:
            updates["llm_text"] = result.llm_text + "\nSome recovery suggestions could not be validated:\n" + "\n".join(diagnostics)
            if isinstance(result, (FailedResult, RejectedResult)):
                updates["error"] = result.error + "\nRecovery guidance also failed: " + diagnostic_summary("\n".join(diagnostics))
            else:
                updates.pop("llm_text")
                updates["output_error"] = (
                    (result.output_error + "\n" if result.output_error else "")
                    + "Some recovery suggestions could not be validated:\n" + "\n".join(diagnostics)
                )
        return result.model_copy(update=updates)

    def _budget_invocation_result(self, result, call, *, budget, turn_id):
        """Bound normal output independently of status and recovery metadata.

        Affordances are validated and capped separately. They are appended
        outside this budget so suggestions cannot crowd out operation facts.
        """
        limit = self._resolve_char_limit(budget) if budget else None
        if limit is None:
            return result
        text = result.llm_text
        if len(text) <= limit:
            return result
        refs = tuple(result.snapshot_refs or ())
        delivery = getattr(result, "context_delivery", None)
        manifest = FileDeliveryManifest.from_dict(delivery) if delivery else None
        managed_snapshot = bool(
            isinstance(result, CompleteResult)
            and call.name == "read_file"
            and call.args.get("file_path")
            and self.result_snapshots is not None
            and self.result_snapshots.lookup_path(str(call.args["file_path"])) is not None
        )
        long_line = bool(manifest) and manifest.operation == "read" and any(
            span.line_length > limit for span in manifest.spans
        )
        if long_line and not managed_snapshot:
            extra = (
                "\nA source line exceeds this delivery budget. Use run_shell with awk/sed/wc "
                "to inspect bounded portions of the original file. edit_file requires complete "
                "source lines; for this case use a focused shell edit (for example sed), checking "
                "the current source before modifying it. Reading the output copy grants no source edit authority."
            )
        elif long_line and managed_snapshot:
            extra = (
                "\nA line of this result snapshot exceeds the delivery budget. View bounded "
                "fragments (for example head/tail portions) instead of whole lines; snapshots "
                "are immutable evidence, not editable sources."
            )
        else:
            extra = ""
        try:
            lifetime = self.logical_context_for_turn(turn_id or call.call_id).execution_lifetime_id
            existing = self.result_snapshots.lookup_path(call.args["file_path"]) if call.args.get("file_path") else None
            # A component attachment is not evidence of complete result coverage.
            # Reads of managed snapshots keep their original source reference;
            # other results must preserve the exact pre-budget result body.
            encoded = text.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            matching = next((item for item in refs if item.digest == digest
                             and item.size_bytes == len(encoded)), None)
            # Snapshot reads intentionally omit source-edit delivery authority;
            # their original reference remains valid without a manifest.
            ref = existing if managed_snapshot else matching
            if ref is None:
                ref = self.result_snapshots.capture(text, call_id=call.call_id,
                    lifetime=lifetime, coverage="complete result text")
            refs = tuple(dict.fromkeys((*refs, ref)))
            hint = render_snapshot_hint(ref) + extra
        except Exception as exc:
            # Never destroy the only copy of a result, including a successful
            # mutation's evidence. Appending leaves file-delivery offsets and
            # existing snapshot references intact.
            updates = {
                "llm_text": text + "\n\nComplete output could not be saved:\n" + exception_report(exc)
                    + "\nThe full result is shown above beyond the output budget because no complete snapshot "
                      "could be saved. Operation status is unchanged; do not repeat side effects to recover output.",
            }
            if isinstance(result, CompleteResult):
                updates["output_error"] = result.output_error or diagnostic_text(str(exc), limit=None)
            return result.model_copy(update=updates)
        preview_allowance = max(0, min(int(budget.preview_chars or 1000), limit - len(hint) - 4))
        marker = "\n... [output omitted] ...\n"
        if preview_allowance < len(marker):
            # The preview cannot fit even its omission marker: the documented
            # minimum-envelope exception applies. Owners compose results
            # facts-first, so a leading bounded structured block (for example
            # a paged session status header) is preserved whole instead of
            # being head-cut into unparsable fragments.
            fact_block = _leading_fact_block(text)
            if fact_block is not None:
                preview, intervals = fact_block, ()
            else:
                preview, intervals = text[:preview_allowance], ()
        else:
            preview, intervals = head_tail(text, preview_allowance)
        # The metadata tail renders from typed fields at the single renderer;
        # the budgeted body carries only the preview plus delivery hint.
        updates = {"llm_text": preview + "\n\n" + hint, "snapshot_refs": refs}
        if isinstance(result, CompleteResult):
            updates["replay_result_ref"] = result.replay_result_ref or (call.call_id if refs else "")
            if manifest:
                spans = []
                for index, (a, b) in enumerate(intervals):
                    part = manifest.slice(a, b)
                    display_offset = 0 if index == 0 else len(preview) - (b - a)
                    if part:
                        spans.extend(replace(span, start_offset=span.start_offset + display_offset,
                            end_offset=span.end_offset + display_offset) for span in part.spans)
                updates["context_delivery"] = replace(manifest, spans=tuple(spans), complete_file=False,
                    inherited_ranges=(), parent_result_ids=(), empty_file=False).to_dict()
        return result.model_copy(update=updates)

    def _normalize_invocation_result_inner(
        self,
        record: CompiledToolRecord,
        call: ToolCallIR,
        raw: Any,
        *,
        budget: ToolCallBudget | None,
        turn_id: str | None,
    ) -> ToolInvocationResult:
        if isinstance(raw, (RejectedResult, FailedResult)):
            return raw.model_copy(update={"llm_text": _with_failure_diagnostics(
                raw.llm_text, raw.details, error=raw.error)})
        if isinstance(raw, CompleteResult):
            return raw
        receipt: EffectReceipt | None = None
        affordances: list[ToolAffordance] = []
        recovery_hint = ""
        llm_text = ""
        context_delivery: dict[str, Any] | None = None
        context_messages: tuple[ToolContextMessageIR, ...] = ()
        from pal.execution.activity import capture_activity_output
        capture_activity_output(record.alias, raw, background_execution=bool(record.binding.descriptor.metadata.get("background_execution")))
        if isinstance(raw, ToolHandlerResult):
            candidate = raw.output
            receipt = raw.effect_receipt
            affordances = normalize_affordances(raw.affordances, limit=None)
            recovery_hint = str(raw.recovery_hint or "")
            llm_text = raw.llm_text
        elif isinstance(raw, CapabilityResult):
            llm_text = str(raw.llm_text or "")
            raw_status = raw.status
            raw_structured = raw.structured
            raw_text = str(raw.text or "")
            raw_receipt = raw.effect_receipt
            raw_delivery = raw.context_delivery
            if isinstance(raw_delivery, dict):
                context_delivery = dict(raw_delivery)
            context_messages = tuple(raw.context_messages or ())
            affordances = normalize_affordances(raw.affordances or (), limit=None)
            recovery_hint = str(raw.recovery_hint or "")
            if isinstance(raw_receipt, EffectReceipt):
                receipt = raw_receipt
            if raw_status != RuntimeStatus.OK:
                outcome = _failure_effect_outcome(record, receipt)
                # Pick the best failure guidance instead of stacking every
                # source (§6.1): handler-provided recovery wins; the declared
                # fallback fills the recovery hint only when nothing more
                # specific exists.
                recovery_hint, owner_affordances = self._safe_failure_guidance(
                    handler_recovery_hint=str(raw.recovery_hint or ""),
                    handler_affordances=affordances,
                    declared_failure_next_steps=record.guidance.failure_next_steps.strip(),
                )
                retry = derive_retry_directive(record.execution, outcome)
                declared_retry = (raw_structured or {}).get("retry")
                if isinstance(declared_retry, str) and declared_retry in {item.value for item in RetryDirective}:
                    retry = RetryDirective(declared_retry)
                result_type = (RejectedResult if (raw_structured or {}).get("kind") == "rejected"
                    and outcome is EffectOutcome.NOT_STARTED else FailedResult)
                declared_error = (raw_structured or {}).get("error")
                if isinstance(declared_error, str) and declared_error.strip():
                    error = diagnostic_summary(declared_error)
                elif isinstance(declared_error, (dict, list)) and declared_error:
                    error = render_structured_for_llm(diagnostic_value(declared_error, summarize=True))
                else:
                    error = raw_text or llm_text
                return result_type(
                    error_code=str((raw_structured or {}).get("error_code") or raw_status or "handler_failed"),
                    error=error,
                    effect=outcome,
                    retry=retry,
                    llm_text=_with_failure_diagnostics(llm_text or raw_text, dict(raw_structured or {}), error=raw_text),
                    affordances=owner_affordances,
                    recovery_hint=recovery_hint,
                    details=dict(raw_structured or {}),
                    snapshot_refs=raw.snapshot_refs,
                    context_messages=context_messages,
                )
            candidate = raw_structured if raw_structured is not None else {"text": raw_text}
            # Only tools/call uses structuredContent. A prompts/get result
            # already has its normalized messages and read effect receipt.
            if (record.is_mcp and record.binding.descriptor.metadata["mcp"]["kind"] == "tool"
                    and isinstance(candidate, dict) and isinstance(candidate.get("raw_result"), dict)):
                mcp_raw = dict(candidate["raw_result"])
                candidate = mcp_raw.get("structuredContent") if record.output_schema != McpToolOutput.model_json_schema(mode="validation") else {
                    "content": list(mcp_raw.get("content") or []),
                    "structured_content": mcp_raw.get("structuredContent"),
                    "is_error": bool(mcp_raw.get("isError")),
                }
                receipt = EffectReceipt(outcome=EffectOutcome.APPLIED, receipt={"mcp_response": True})
        else:
            candidate = raw

        if receipt is not None:
            outcome = receipt.outcome
        elif record.execution.effect_kind is EffectKind.NONE:
            outcome = EffectOutcome.NONE
        elif record.requires_effect_receipt:
            outcome = EffectOutcome.UNKNOWN
            return FailedResult(
                error_code="missing_effect_receipt",
                error=f"effectful handler for {record.alias} returned no effect receipt",
                effect=outcome,
                retry=derive_retry_directive(record.execution, outcome),
                llm_text=(f"Effect outcome is unknown for {record.alias}; the handler returned no effect receipt. "
                          "Reconcile before retrying.\nUnvalidated tool result:\n" + render_structured_for_llm({
                              "output": diagnostic_value(candidate), "llm_text": diagnostic_text(llm_text, limit=None),
                          })),
                details={"raw_output_text": render_structured_for_llm(candidate), "raw_llm_text": llm_text},
                snapshot_refs=raw.snapshot_refs if isinstance(raw, CapabilityResult) else (),
                recovery_hint=recovery_hint,
                affordances=affordances,
                context_messages=context_messages,
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
                output_model = validate_output(record.output_model, candidate)
                output = dump_output(output_model)
        except (ValidationError, JsonSchemaValidationError, TypeError) as exc:
            diagnostic = exception_report(exc)
            return FailedResult(
                error_code="output_validation_failed",
                error=exception_summary(exc),
                effect=outcome,
                retry=derive_retry_directive(record.execution, outcome),
                llm_text=(f"Tool output contract error for {record.alias}; effect={outcome.value}.\n"
                          f"Validation error:\n{diagnostic}\n"
                          "This is a tool/provider output error, not a task argument error. "
                          "Inspect the captured result and repair the tool/provider contract; "
                          "do not repeat side effects to retrieve this output.\n"
                          "Unvalidated tool result:\n" + render_structured_for_llm({
                              "output": diagnostic_value(candidate), "llm_text": diagnostic_text(llm_text, limit=None),
                          })),
                details={"output_schema": record.output_schema,
                         "raw_output_text": render_structured_for_llm(candidate),
                         "raw_llm_text": llm_text},
                recovery_hint="Repair the tool/provider output contract, not the task arguments. Do not repeat side effects to recover output.",
                context_messages=context_messages,
            )
        # Handler text is data, including leading/trailing whitespace. Only
        # Pal-owned structured serialization may change presentation.
        rendered = llm_text or render_structured_for_llm(output)
        refs = raw.snapshot_refs if isinstance(raw, CapabilityResult) else ()
        return CompleteResult(
            output=output, effect=outcome, llm_text=rendered,
            affordances=affordances, recovery_hint=recovery_hint, context_delivery=context_delivery,
            snapshot_refs=refs, replay_result_ref=call.call_id if refs else "", context_messages=context_messages,
        )

    def bind_result_history(self, memory_service) -> None:
        if memory_service is not None:
            self.result_snapshots.bind_history(memory_service.history)

    def configure_runtime_root(self, root) -> None:
        root = Path(root)
        if self.result_snapshots.references():
            raise RuntimeError("Cannot move runtime storage while output snapshots are live")
        self.runtime_root = root
        self.result_snapshots = ResultSnapshotStore(root)


    @staticmethod
    def _rejected_error_result(exc: ToolRejectedError) -> RejectedResult:
        text = exception_report(exc)
        if exc.details:
            text += "\nFailure details: " + render_structured_for_llm(diagnostic_value(exc.details))
        return rejection(
            exc.error_code,
            text,
            retry=exc.retry,
            affordances=normalize_affordances(exc.affordances, limit=None),
            details=dict(exc.details),
            recovery_hint=str(getattr(exc, "recovery_hint", "") or ""),
        ).model_copy(update={"error": exception_summary(exc)})

    @staticmethod
    def _safe_failure_guidance(
        *,
        handler_recovery_hint: str,
        handler_affordances: list[ToolAffordance],
        declared_failure_next_steps: str,
    ) -> tuple[str, list[ToolAffordance]]:
        try:
            return resolve_failure_guidance(
                handler_recovery_hint=handler_recovery_hint,
                handler_affordances=handler_affordances,
                declared_failure_next_steps=declared_failure_next_steps,
            )
        except Exception as exc:
            _LOGGER.warning("Unable to resolve failure guidance; retaining operation failure", exc_info=True)
            hint = handler_recovery_hint or declared_failure_next_steps
            diagnostic = "Recovery guidance resolution also failed:\n" + exception_report(exc)
            return ((hint + "\n" if hint else "") + diagnostic), []

    @staticmethod
    def _handler_exception_result(record: CompiledToolRecord, exc: Exception) -> FailedResult:
        receipt = getattr(exc, "effect_receipt", None)
        outcome = _failure_effect_outcome(record, receipt if isinstance(receipt, EffectReceipt) else None)
        retry = exc.retry if isinstance(exc, ToolExecutionError) else None
        if not isinstance(retry, RetryDirective):
            retry = derive_retry_directive(record.execution, outcome)
        diagnostic = exception_report(exc)
        details = dict(getattr(exc, "details", {}) or {})
        llm_text = f"Tool {record.alias} failed ({type(exc).__name__}); effect={outcome.value}.\n{diagnostic}"
        if details:
            llm_text += "\nFailure details: " + render_structured_for_llm(diagnostic_value(details))
        recovery_hint, affordances = ExecutionRuntime._safe_failure_guidance(
            handler_recovery_hint=str(getattr(exc, "recovery_hint", "") or ""),
            handler_affordances=list(getattr(exc, "affordances", ()) or ()),
            declared_failure_next_steps=record.guidance.failure_next_steps.strip(),
        )
        return FailedResult(
            error_code=str(getattr(exc, "error_code", "handler_exception") or "handler_exception"),
            error=exception_summary(exc),
            effect=outcome,
            retry=retry,
            llm_text=llm_text,
            affordances=affordances,
            recovery_hint=recovery_hint,
            details=details,
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
                replay_result_ref=str(result.replay_result_ref or ""),
                snapshot_refs=result.snapshot_refs,
                context_messages=tuple(result.context_messages),
            )
        return ToolExecutionResult(name=alias, ok=False, text=rendered,
            structured=result.model_dump(mode="json"), call_id=call_id,
            llm_text=rendered, status=result.error_code, invocation_result=result,
            snapshot_refs=result.snapshot_refs, replay_result_ref=call_id if result.snapshot_refs else "",
            context_messages=tuple(result.context_messages))

    @staticmethod
    def _invocation_metadata_values(
        result: ToolInvocationResult,
        *,
        recovery_hint: str | None = None,
        affordances: list[ToolAffordance] | None = None,
    ) -> dict[str, Any]:
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
        elif result.output_error:
            metadata["delivery_error"] = result.output_error
        hint = (
            str(result.recovery_hint or "") if recovery_hint is None else recovery_hint
        ).strip()
        if hint:
            metadata["recovery"] = hint
        actions = result.affordances if affordances is None else affordances
        if actions:
            metadata["affordances"] = [item.model_dump(mode="json") for item in actions]
        return metadata

    @classmethod
    def _rendered_guidance_tail(
        cls, result: ToolInvocationResult, *, include_affordances: bool = True
    ) -> str:
        """The deterministic metadata suffix the renderer appends to the body."""
        metadata = cls._invocation_metadata_values(
            result, affordances=None if include_affordances else []
        )
        if metadata == {"kind": "complete", "effect": EffectOutcome.NONE.value}:
            return ""
        return f"\n\nTool result metadata: {render_structured_for_llm(metadata)}"

    @classmethod
    def _render_invocation_for_llm(cls, result: ToolInvocationResult) -> str:
        base = str(result.llm_text or "")
        return base + cls._rendered_guidance_tail(result)

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
        if self.activity_decorator is not None:
            return await self.activity_decorator.invoke(
                call,
                lambda: self._execute_tool_async(call, allow_tools=allow_tools, budget=budget, turn_id=turn_id),
                turn_id=turn_id,
            )
        return await self._execute_tool_async(call, allow_tools=allow_tools, budget=budget, turn_id=turn_id)

    async def _execute_tool_async(
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

    def _invocation_gate(self, name: object, *, asynchronous: bool = False):
        if _is_package_job_tool(name):
            return contextlib.nullcontext()
        if _is_plugin_lifecycle_tool(name):
            return self.lifecycle_gate.write_async() if asynchronous else self.lifecycle_gate.write()
        return self.lifecycle_gate.read_async() if asynchronous else self.lifecycle_gate.read()

    def call_registered(self, call: CapabilityCall) -> CapabilityResult:
        gate = self._invocation_gate(call.name)
        with gate:
            return self._call_registered_unlocked(call)

    def _call_registered_unlocked(self, call: CapabilityCall) -> CapabilityResult:
        generation = self._registry_generation
        canonical_path = generation.capability_index.canonical_path_for(call.name)
        call = CapabilityCall(name=canonical_path, args=dict(call.args), meta=dict(call.meta))
        bound = self._resolve_binding(call, generation=generation)
        if isinstance(bound, CapabilityResult):
            return bound
        result = bound.callable(call)
        if inspect.isawaitable(result):
            raise RuntimeError(f"capability requires async execution: {canonical_path}")
        return result

    async def call_registered_async(self, call: CapabilityCall) -> CapabilityResult:
        gate = self._invocation_gate(call.name, asynchronous=True)
        async with gate:
            return await self._call_registered_async_unlocked(call)

    async def _call_registered_async_unlocked(self, call: CapabilityCall) -> CapabilityResult:
        generation = self._registry_generation
        canonical_path = generation.capability_index.canonical_path_for(call.name)
        call = CapabilityCall(name=canonical_path, args=dict(call.args), meta=dict(call.meta))
        bound = self._resolve_binding(call, generation=generation)
        if isinstance(bound, CapabilityResult):
            return bound
        if bound.async_callable is not None:
            result = bound.async_callable(call)
            return await result if inspect.isawaitable(result) else result
        result = await self._call_sync_handler_async(bound, call)
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

        target_id = str(call.args.get("target_id") or "").strip()
        if not target_id:
            for descriptor_name in captured.capability_index.by_canonical.get(call.name, ()):
                descriptor = captured.capability_index.records.get(descriptor_name)
                if descriptor is None:
                    continue
                target_argument = str(descriptor.metadata.get("target_argument") or "")
                if target_argument:
                    target_id = str(call.args.get(target_argument) or "").strip()
                    if target_id:
                        break
        target_id = target_id or SINGLETON_TARGET
        bound = captured.canonical_bindings.get(call.name, target_id)
        if bound is not None:
            return bound
        matching = captured.capability_index.by_canonical.get(call.name, [])
        if matching and target_id == SINGLETON_TARGET:
            descriptors = [captured.capability_index.records[record_id] for record_id in matching]
            target_argument = next(
                (
                    str(descriptor.metadata.get("target_argument") or "")
                    for descriptor in descriptors
                    if descriptor.metadata.get("target_argument")
                ),
                "target_id",
            )
            instance_targets = sorted(
                {
                    candidate_target
                    for candidate_path, candidate_target in captured.canonical_bindings.actions
                    if candidate_path == call.name and candidate_target != SINGLETON_TARGET
                }
            )
            if instance_targets:
                return _target_name_required_result(
                    canonical_path=call.name,
                    argument=target_argument,
                    available_names=instance_targets,
                )
        return CapabilityResult(
            status=RuntimeStatus.ERROR,
            text=f"unknown capability: {call.name}",
            llm_text=f"unknown capability: {call.name}",
        )

    def _resolve_descriptor(self, name: str) -> CapabilityDescriptor | CapabilityResult | None:
        candidates: list[CapabilityDescriptor] = []
        raw = str(name or "").strip()
        direct = self.compiled_capability_index.records.get(raw)
        if direct is not None:
            return direct
        canonical_path = self.resolve_capability_address(raw)
        candidates.extend(
            self.compiled_capability_index.records[record_id]
            for record_id in self.compiled_capability_index.by_canonical.get(canonical_path, [])
            if record_id in self.compiled_capability_index.records
        )
        if not candidates:
            return None
        unique: dict[str, CapabilityDescriptor] = {descriptor.name: descriptor for descriptor in candidates}
        candidates = list(unique.values())
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
            return _target_name_required_result(name=name, argument="target_id", available_names=instance_targets)
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
            return self.call_registered(
                CapabilityCall(
                    name=call.name,
                    args=dict(call.args),
                    meta={**dict(call.meta), "execution_runtime": self},
                )
            )
        except Exception as exc:
            return self._capability_exception_result(call, exc)

    async def execute_async(self, call: CapabilityCall) -> CapabilityResult:
        try:
            return await self.call_registered_async(
                CapabilityCall(
                    name=call.name,
                    args=dict(call.args),
                    meta={**dict(call.meta), "execution_runtime": self},
                )
            )
        except Exception as exc:
            return self._capability_exception_result(call, exc)

    @staticmethod
    def _capability_exception_result(call: CapabilityCall, exc: Exception) -> CapabilityResult:
        rejected = isinstance(exc, ToolRejectedError)
        declared = isinstance(exc, (ToolRejectedError, ToolExecutionError))
        diagnostic = exception_report(exc)
        text = diagnostic if rejected else f"Capability execution failed: {diagnostic}"
        details = dict(exc.details) if declared else {}
        if details:
            text += "\nFailure details: " + render_structured_for_llm(diagnostic_value(details))
        structured = {
            **details,
            "kind": "rejected" if rejected else "failed",
            "error_code": exc.error_code if declared else "handler_exception",
            "capability": call.name,
        }
        if "error" in structured:
            structured["diagnostic"] = diagnostic
        else:
            structured["error"] = diagnostic
        retry = exc.retry if declared else None
        if isinstance(retry, RetryDirective):
            structured["retry"] = retry.value
        return CapabilityResult(
            status=RuntimeStatus.INVALID if rejected else RuntimeStatus.ERROR,
            text=text, llm_text=text, structured=structured,
            effect_receipt=(EffectReceipt(outcome=EffectOutcome.NOT_STARTED) if rejected else
                            exc.effect_receipt if isinstance(exc, ToolExecutionError) else None),
            recovery_hint=exc.recovery_hint if declared else "",
            affordances=tuple(exc.affordances) if declared else (),
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


def _target_name_required_result(
    *,
    available_names: list[str],
    argument: str,
    canonical_path: str = "",
    name: str = "",
) -> CapabilityResult:
    payload = {
        "error_code": "target_name_required",
        "argument": argument,
        "available_names": list(available_names),
    }
    if canonical_path:
        payload["canonical_path"] = canonical_path
    if name:
        payload["name"] = name
    target_text = ", ".join(available_names) if available_names else "(none)"
    capability = canonical_path or name or "this capability"
    return CapabilityResult(
        status=RuntimeStatus.INVALID,
        text=f"{argument} is required for this capability",
        structured=payload,
        llm_text=(
            f"{argument} is required for {capability}. "
            f"Available names: {target_text}. "
            f"Retry with args.{argument} set to one of these names."
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
            value = str(record.get(field_name) or "")
            if not value:
                continue
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


def _target_discovery_alias(descriptor: CapabilityDescriptor) -> str:
    configured = str(descriptor.metadata.get("target_discovery_alias") or "").strip()
    if configured:
        return configured
    if descriptor.target_kind == "endpoint":
        return "list_channel_endpoints"
    if descriptor.target_kind == "proactive_task":
        return "list_proactive_tasks"
    if descriptor.target_kind == "provider":
        return {
            "memory": "list_memory_providers",
            "web_search": "list_web_search_providers",
        }.get(descriptor.module_id, "")
    return ""


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
        "guidance": descriptor.guidance.model_dump(mode="json"),
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
