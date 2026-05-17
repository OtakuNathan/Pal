from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
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
        description="Show the current active memory provider used by op_memory_recall, op_memory_write, op_memory_update, and op_memory_delete",
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
            "Recall durable memory records from the active memory provider. "
            "The result renders each item as [mem_ref]: text. Use mem_ref only for op_memory_update or op_memory_delete."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "level": {"type": "string"},
                "queries": {"type": "array", "items": {"type": "string"}},
                "topic_scope": {"type": "array", "items": {"type": "string"}},
                "task_id": {"type": "string"},
                "limit": {"type": "integer"},
                "kind": {"type": "string"},
                "scope": {"type": "string"},
                "view": {"type": "string", "enum": ["summary", "origin"]},
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
            "Commit a durable memory record to the active memory provider. "
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
            "mem_ref is the opaque ref returned by op_memory_recall; do not invent it."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "mem_ref": {"type": "string", "description": "Opaque memory ref returned by op_memory_recall."},
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
            "mem_ref is the opaque ref returned by op_memory_recall; do not invent it. "
            "Use only when the user explicitly asks to forget/delete a specific memory or a clearly invalid record."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "mem_ref": {"type": "string", "description": "Opaque memory ref returned by op_memory_recall."},
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
        ports={"memory": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle
