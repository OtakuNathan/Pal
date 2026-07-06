from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pal.control.contracts import ControlAction
from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.contracts import CapabilityCall
from pal.memory.candidates import l3_commit_args_from_memory_candidate
from pal.memory.contracts import L3CommitRequest, L3CorrectRequest, L3DeleteRequest, MemoryQuery
from pal.memory.rendering import (
    build_mutation_structured_payload,
    build_recall_structured_payload,
    normalize_recall_view,
    render_mutation_result_for_llm,
    render_recall_result_for_llm,
)
from pal.memory.service import MemoryService
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


def _read_mem_ref(args: dict[str, Any]) -> str:
    return str(args.get("mem_ref") or args.get("document_id") or "").strip()


def _memory_title_from_summary(summary: str) -> str:
    normalized = " ".join(str(summary or "").strip().split())
    if not normalized:
        return "Memory"
    return normalized[:96].rstrip()


@dataclass(frozen=True)
class MemorySnapshot:
    l1_count: int
    l2_count: int
    active_l3_provider: str
    available_l3_providers: list[str]


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:memory",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:memory",
    target_kind="module",
)
@dataclass
class MemoryIntrospectionProvider:
    service: MemoryService
    context: MainContext
    module_id: str = "memory"

    async def handle_memory_candidate_decision_async(self, action: ControlAction) -> str:
        decision = str(action.args.get("decision") or "").strip().lower()
        if decision == "reject":
            return "Memory candidates discarded."
        if decision == "edit":
            return "Memory candidate absorption paused. Edit and resubmit the candidates when ready."
        if decision != "accept":
            return "Unknown memory candidate decision."
        memory_candidates = _dict_list(action.args.get("memory_candidates"))
        if not memory_candidates:
            return "No memory candidates to commit."
        runtime = getattr(self.context, "execution_runtime", None)
        if runtime is None or "op_memory_write" not in getattr(runtime, "capabilities", {}):
            return (
                f"Memory candidates accepted ({len(memory_candidates)} reviewed; "
                "0 committed; memory write unavailable)."
            )
        source_kind = str(action.args.get("source_kind") or "").strip()
        source_ref = str(action.args.get("source_ref") or action.target_id or "").strip()
        default_scope = "task" if source_kind == "minion" else "system"
        fallback_task_id = source_ref if default_scope == "task" else ""
        committed = 0
        skipped = 0
        for candidate in memory_candidates:
            args = l3_commit_args_from_memory_candidate(
                candidate,
                default_scope=default_scope,
                fallback_task_id=fallback_task_id,
                source_kind=source_kind,
                source_ref=source_ref,
            )
            if not args:
                skipped += 1
                continue
            result = await runtime.execute_async(CapabilityCall(name="op_memory_write", args=args))
            if str(getattr(result, "status", "") or "") == RuntimeStatus.OK:
                committed += 1
            else:
                skipped += 1
        suffix = f"; {skipped} skipped" if skipped else ""
        return f"Memory candidates accepted ({len(memory_candidates)} reviewed; {committed} committed{suffix})."

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show memory runtime state")
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_memory(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory snapshot",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("Memory snapshot", snapshot.__dict__),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list_providers",
        description="List registered L3 providers",
    )
    def list_providers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = []
        for provider_id in sorted(self.context.execution_runtime.l3_plugin_registry.plugins):
            provider = self.context.execution_runtime.l3_plugin_registry.get(provider_id)
            items.append(
                {
                    "provider_id": provider_id,
                    "module_id": getattr(provider, "module_id", f"l3.{provider_id}") if provider is not None else f"l3.{provider_id}",
                    "mounted": bool(getattr(provider, "mounted", True)) if provider is not None else False,
                }
            )
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory l3 providers",
            structured={"items": items},
            llm_text=render_titled_structured_for_llm("Memory L3 providers", {"items": items}),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="active_provider",
        description="Show the current active memory provider used by recall_memory, write_memory, update_memory, and delete_memory",
    )
    def active_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        provider_id = self.service.l3_selector.active_provider_id
        provider = self.context.execution_runtime.l3_plugin_registry.get(provider_id)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory active l3 provider",
            structured={
                "provider_id": provider_id,
                "module_id": getattr(provider, "module_id", f"l3.{provider_id}") if provider is not None else f"l3.{provider_id}",
                "mounted": bool(getattr(provider, "mounted", True)) if provider is not None else False,
            },
            llm_text=render_titled_structured_for_llm(
                "Memory active L3 provider",
                {
                    "provider_id": provider_id,
                    "module_id": getattr(provider, "module_id", f"l3.{provider_id}") if provider is not None else f"l3.{provider_id}",
                    "mounted": bool(getattr(provider, "mounted", True)) if provider is not None else False,
                },
            ),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="recall",
        action_name="recall",
        description=(
            "Recall durable memory records from the active memory provider. Use this before acting when the task depends "
            "on prior Pal decisions, user preferences, project history, custom Pal/project terms, known failures, repair "
            "lessons, or before writing/updating/deleting memory, behavior guidance, or skills. Do not use it for current "
            "runtime state; inspect live runtime/capabilities instead. Do not use it for current external facts; verify "
            "externally instead. Use targeted queries with limit 3-5 by default. Results render each item as "
            "[mem_ref]: text. mem_ref is opaque; prefixes such as fact: or case: are part of the ref and must be copied "
            "exactly when using update_memory or delete_memory."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "level": {"type": "string", "description": "Recall temperature such as warm; omit unless narrowing behavior is needed."},
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Targeted query strings for the user/project fact, prior decision, repair lesson, or memory candidate.",
                },
                "topic_scope": {"type": "array", "items": {"type": "string"}, "description": "Optional topic narrowing terms."},
                "task_id": {"type": "string"},
                "limit": {"type": "integer", "description": "Maximum memories to return; use 3-5 by default."},
                "kind": {"type": "string", "description": "Optional memory kind filter, such as fact or case."},
                "scope": {"type": "string", "description": "Optional scope filter, such as system or task."},
                "view": {"type": "string", "enum": ["summary", "origin"], "description": "summary is compact; origin includes source/origin detail when available."},
            },
        },
    )
    def recall(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self.service.l3_selector.resolve()
        query = MemoryQuery(
            level=str(call.args.get("level") or "warm"),
            queries=[str(value) for value in list(call.args.get("queries") or [])],
            topic_scope=[str(value) for value in list(call.args.get("topic_scope") or [])],
            task_id=str(call.args.get("task_id")) if call.args.get("task_id") is not None else None,
            limit=int(call.args.get("limit") or 8),
            kind=str(call.args.get("kind")) if call.args.get("kind") is not None else None,
            scope=str(call.args.get("scope")) if call.args.get("scope") is not None else None,
            view=normalize_recall_view(call.args.get("view")),
        )
        result = provider.recall(query)
        provider_id = str(getattr(provider, "provider_id", self.service.l3_selector.active_provider_id) or "")
        payload = build_recall_structured_payload(provider_id=provider_id, query=query, result=result, view=query.view)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory recall result",
            structured=payload,
            llm_text=render_recall_result_for_llm(provider_id=provider_id, query=query, result=result, view=query.view),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="commit",
        action_name="write",
        description=(
            "Commit a new durable memory record only when the user explicitly asks Pal to remember/save it or states a "
            "clear durable fact/preference with low ambiguity. Before using this tool, call recall_memory with the "
            "candidate summary/search_text and limit 3-5. If a recalled [mem_ref] is semantically the same record, an "
            "older version, already covers the candidate, or is the memory being corrected, use update_memory with that "
            "complete mem_ref instead. Do not write duplicate memories. Do not invent mem_ref values; prefixes such as "
            "fact: or case: are part of the ref. "
            "Use summary for prompt-ready memory text and search_text for source-of-truth retrieval text."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "fact or case"},
                "summary": {"type": "string", "description": "Concise memory text for future prompt display."},
                "search_text": {"type": "string", "description": "Source-of-truth text used for retrieval indexing."},
                "scope": {"type": "string", "description": "system or task"},
                "task_id": {"type": "string"},
                "canonical_key": {"type": "string"},
                "payload": {"type": "object"},
                "topics": {"type": "array", "items": {"type": "string"}},
                "situation_text": {"type": "string"},
                "task_text": {"type": "string"},
                "action_text": {"type": "string"},
                "result_text": {"type": "string"},
            },
            "required": ["kind", "summary", "search_text"],
        },
    )
    def write(self, call: IntrospectionCall) -> IntrospectionResult:
        kind = str(call.args.get("kind") or "").strip()
        summary = str(call.args.get("summary") or "").strip()
        search_text = str(call.args.get("search_text") or "").strip()
        if not kind:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="kind is required", llm_text="kind is required")
        if not summary:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="summary is required", llm_text="summary is required")
        if not search_text:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="search_text is required", llm_text="search_text is required")
        provider = self.service.l3_selector.resolve()
        result = provider.commit(
            L3CommitRequest(
                kind=kind,
                title=_memory_title_from_summary(summary),
                summary=summary,
                search_text=search_text,
                scope=str(call.args.get("scope") or "system"),
                task_id=str(call.args.get("task_id")) if call.args.get("task_id") is not None else None,
                canonical_key=str(call.args.get("canonical_key")) if call.args.get("canonical_key") is not None else None,
                payload=dict(call.args.get("payload") or {}),
                topics=[str(value) for value in list(call.args.get("topics") or [])],
                situation_text=str(call.args.get("situation_text") or ""),
                task_text=str(call.args.get("task_text") or ""),
                action_text=str(call.args.get("action_text") or ""),
                result_text=str(call.args.get("result_text") or ""),
            )
        )
        payload = build_mutation_structured_payload(result)
        return IntrospectionResult(
            status=result.status,
            text="memory commit result",
            structured=payload,
            llm_text=render_mutation_result_for_llm("commit", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="correct",
        action_name="update",
        description=(
            "Update an existing durable memory record in the active memory provider. "
            "Use this instead of write_memory when a recalled memory is being corrected, superseded, or already covers "
            "the candidate. mem_ref is the opaque ref returned by recall_memory; copy it exactly, including prefixes "
            "such as fact: or case:. Do not invent or shorten mem_ref values."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "mem_ref": {
                    "type": "string",
                    "description": "Opaque memory ref returned by recall_memory. Copy the complete value, including prefixes such as fact: or case:.",
                },
                "summary": {"type": "string"},
                "search_text": {"type": "string"},
                "payload_patch": {"type": "object"},
                "topics": {"type": "array", "items": {"type": "string"}},
                "situation_text": {"type": "string"},
                "task_text": {"type": "string"},
                "action_text": {"type": "string"},
                "result_text": {"type": "string"},
            },
            "required": ["mem_ref"],
        },
    )
    def update(self, call: IntrospectionCall) -> IntrospectionResult:
        mem_ref = _read_mem_ref(call.args)
        provider = self.service.l3_selector.resolve()
        result = provider.correct(
            L3CorrectRequest(
                document_id=mem_ref,
                summary=str(call.args.get("summary")) if call.args.get("summary") is not None else None,
                search_text=str(call.args.get("search_text")) if call.args.get("search_text") is not None else None,
                payload_patch=dict(call.args.get("payload_patch") or {}),
                topics=[str(value) for value in list(call.args.get("topics") or [])] if call.args.get("topics") is not None else None,
                situation_text=str(call.args.get("situation_text")) if call.args.get("situation_text") is not None else None,
                task_text=str(call.args.get("task_text")) if call.args.get("task_text") is not None else None,
                action_text=str(call.args.get("action_text")) if call.args.get("action_text") is not None else None,
                result_text=str(call.args.get("result_text")) if call.args.get("result_text") is not None else None,
            )
        )
        payload = build_mutation_structured_payload(result)
        return IntrospectionResult(
            status=result.status,
            text="memory update result",
            structured=payload,
            llm_text=render_mutation_result_for_llm("update", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="delete",
        action_name="delete",
        description=(
            "Delete an existing durable memory record from the active memory provider. "
            "Use only when the user explicitly asks to forget/delete a specific memory or approves deleting a clearly "
            "invalid record. mem_ref is the opaque ref returned by recall_memory; copy it exactly, including prefixes "
            "such as fact: or case:. Do not invent or shorten mem_ref values."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "mem_ref": {
                    "type": "string",
                    "description": "Opaque memory ref returned by recall_memory. Copy the complete value, including prefixes such as fact: or case:.",
                },
                "reason": {"type": "string"},
            },
            "required": ["mem_ref"],
        },
    )
    def delete(self, call: IntrospectionCall) -> IntrospectionResult:
        mem_ref = _read_mem_ref(call.args)
        provider = self.service.l3_selector.resolve()
        result = provider.delete(L3DeleteRequest(document_id=mem_ref, reason=str(call.args.get("reason") or "")))
        payload = build_mutation_structured_payload(result)
        return IntrospectionResult(
            status=result.status,
            text="memory delete result",
            structured=payload,
            llm_text=render_mutation_result_for_llm("delete", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_active_provider",
        description="Switch the active L3 provider for memory recall",
        args_schema={
            "type": "object",
            "properties": {
                "active_provider_id": {"type": "string"},
            },
            "required": ["active_provider_id"],
        },
    )
    def set_active_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        provider_id = str(call.args.get("active_provider_id") or "").strip()
        if not provider_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="active_provider_id is required",
                llm_text="active_provider_id is required",
            )
        if self.context.execution_runtime.l3_plugin_registry.get(provider_id) is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="unknown l3 provider",
                structured={"active_provider_id": provider_id},
                llm_text="unknown l3 provider",
            )
        self.service.l3_selector.active_provider_id = provider_id
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory active l3 provider updated",
            structured={"active_provider_id": provider_id},
            llm_text=render_titled_structured_for_llm("Memory active L3 provider updated", {"active_provider_id": provider_id}),
        )


def inspect_memory(provider: MemoryIntrospectionProvider) -> MemorySnapshot:
    service = provider.service
    return MemorySnapshot(
        l1_count=len(service.l1_store.items),
        l2_count=len(service.l2_store.items),
        active_l3_provider=service.l3_selector.active_provider_id,
        available_l3_providers=sorted(provider.context.execution_runtime.l3_plugin_registry.plugins),
    )


def register_with_core(context: MainContext, service: MemoryService, *, config: Any = None) -> ModuleHandle:
    from pal.memory.prompt import MemoryPromptFragmentProvider

    provider = MemoryIntrospectionProvider(service=service, context=context)
    prompt_provider = MemoryPromptFragmentProvider(config=config)
    handle = ModuleHandle(
        module_id="memory",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        control_action_handlers={
            "memory_candidate_decision": provider.handle_memory_candidate_decision_async,
        },
        ports={"memory": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]
