from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pal.control.contracts import ControlAction
from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.contracts import CapabilityCall
from pal.memory.candidates import l3_commit_args_from_memory_candidate, memory_star_from_args, star_text_fields
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


def _read_task_id(args: dict[str, Any]) -> str | None:
    task_id = str(args.get("task_id") or "").strip()
    return task_id or None


def _recall_scope_for_task_id(task_id: str | None) -> str | None:
    return "task" if task_id else None


def _memory_title_from_summary(summary: str) -> str:
    normalized = " ".join(str(summary or "").strip().split())
    if not normalized:
        return "Memory"
    return normalized[:96].rstrip()


MEMORY_STAR_SCHEMA = {
    "type": "object",
    "description": (
        "Required when kind='case'; omit for fact memories. STAR case detail for reusable failures, repairs, or task lessons."
    ),
    "properties": {
        "situation": {
            "type": "string",
            "description": "The situation or failure context that future Pal should recognize.",
        },
        "task": {
            "type": "string",
            "description": "The task or objective Pal was trying to complete in that situation.",
        },
        "action": {
            "type": "string",
            "description": "The action, repair, or decision that mattered.",
        },
        "result": {
            "type": "string",
            "description": "The outcome, lesson, or observed result that makes the case reusable.",
        },
    },
    "required": ["situation", "task", "action", "result"],
}


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
        has_capability = getattr(runtime, "has_registered_capability", None)
        if runtime is None or not callable(has_capability) or not has_capability("op_memory_write"):
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
        description="Show the current active memory provider used by recall_memory, remember_memory, update_memory, and forget_memory",
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
            "lessons, or before creating/changing/forgetting durable memory records. When an error, regression, failed "
            "repair, repeated pitfall, or unfamiliar debugging situation appears, prefer a targeted recall with kind='case' "
            "to check prior failures and fixes before improvising. Memory is Pal's remembered facts, "
            "not behavior guidance: behavior is the condition-reflex layer for when situation X should trigger route/action Y. "
            "Do not use memory for current runtime state; inspect live runtime/capabilities instead. Do not use it for current external facts; verify "
            "externally instead. Use targeted queries with limit 3-5 by default. Results render each item as "
            "[mem_ref]: text. mem_ref is opaque; prefixes such as fact: or case: are part of the ref and must be copied "
            "exactly when using update_memory or forget_memory."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "One to three focused natural-language search strings for the remembered fact, preference, "
                        "project context, prior decision, repair lesson, failure case, or candidate memory. Include concrete names, "
                        "modules, error text, symptoms, failed fixes, or user terms when known. Do not paste large raw context; summarize the lookup target."
                    ),
                },
                "topic_scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional short topic keywords that narrow retrieval, such as a project, subsystem, user preference "
                        "area, or failure domain. This is semantic narrowing, not the storage scope; do not use system/task here."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Optional exact task, work order, run, or minion task identifier from current context. When provided, "
                        "recall is narrowed to task-scoped memories for that task. Do not invent or guess task ids."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Maximum memories to return. Use 3-5 by default; use a larger value only when comparing several possible matches.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["fact", "case"],
                    "description": (
                        "Optional memory type filter. Use fact for stable facts, preferences, project context, or prior "
                        "decisions. Use case for prior failures, debugging attempts, repair lessons, task experience, "
                        "or when current work hits an error and prior pitfall/fix experience may exist."
                    ),
                },
                "view": {
                    "type": "string",
                    "enum": ["summary", "origin"],
                    "description": (
                        "Use summary by default for normal work. Use origin only when provenance, source text, or extra "
                        "detail is needed to resolve a conflict, update/delete a memory safely, or audit where the memory came from."
                    ),
                },
            },
        },
    )
    def recall(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self.service.l3_selector.resolve()
        task_id = _read_task_id(call.args)
        query = MemoryQuery(
            level=str(call.args.get("level") or "warm"),
            queries=[str(value) for value in list(call.args.get("queries") or [])],
            topic_scope=[str(value) for value in list(call.args.get("topic_scope") or [])],
            task_id=task_id,
            limit=int(call.args.get("limit") or 8),
            kind=str(call.args.get("kind")) if call.args.get("kind") is not None else None,
            scope=_recall_scope_for_task_id(task_id),
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
            "Remember a new durable memory record only when the user explicitly asks Pal to remember/save it or states a "
            "clear durable fact/preference with low ambiguity. Use for facts, preferences, project context, prior decisions, "
            "and reusable repair lessons. Do not use for behavior rules about when Pal should take a route/action; use learn_behavior for that. "
            "Before using this tool, call recall_memory with the "
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
                "kind": {
                    "type": "string",
                    "enum": ["fact", "case"],
                    "description": (
                        "Use fact for stable facts, preferences, project context, or decisions. "
                        "Use case for reusable task/failure/repair lessons; case requires star."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "Concise prompt-ready memory text future Pal can read directly.",
                },
                "search_text": {
                    "type": "string",
                    "description": (
                        "Retrieval/source text with concrete names, symptoms, decisions, or wording. "
                        "This can be longer than summary but should not be raw unrelated context."
                    ),
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short semantic topic tags such as project, subsystem, preference area, or failure domain.",
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Optional exact task, work order, run, or minion task id from current context. "
                        "Providing it binds this memory to that task scope. Do not invent task ids."
                    ),
                },
                "star": MEMORY_STAR_SCHEMA,
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
        if kind not in {"fact", "case"}:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="kind must be fact or case", llm_text="kind must be fact or case")
        star, star_error = memory_star_from_args(call.args)
        if star_error:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text=star_error, llm_text=star_error)
        if kind == "case" and not star:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="kind=case requires star with situation, task, action, and result",
                llm_text="kind=case requires star with situation, task, action, and result",
            )
        if kind != "case" and star:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="star is only valid when kind=case",
                llm_text="star is only valid when kind=case",
            )
        task_id = _read_task_id(call.args)
        payload = dict(call.args.get("payload") or {})
        if star:
            payload.update(star)
        star_fields = star_text_fields(star) if star else {}
        provider = self.service.l3_selector.resolve()
        result = provider.commit(
            L3CommitRequest(
                kind=kind,
                title=_memory_title_from_summary(summary),
                summary=summary,
                search_text=search_text,
                scope="task" if task_id else str(call.args.get("scope") or "system"),
                task_id=task_id,
                canonical_key=str(call.args.get("canonical_key")) if call.args.get("canonical_key") is not None else None,
                payload=payload,
                topics=[str(value) for value in list(call.args.get("topics") or [])],
                situation_text=star_fields.get("situation_text", ""),
                task_text=star_fields.get("task_text", ""),
                action_text=star_fields.get("action_text", ""),
                result_text=star_fields.get("result_text", ""),
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
            "Use this instead of remember_memory when a recalled memory is being corrected, superseded, or already covers "
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
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional replacement topic tags for retrieval narrowing.",
                },
                "star": {
                    **MEMORY_STAR_SCHEMA,
                    "description": (
                        "Optional full STAR replacement for a case memory. If provided, all four fields are required. "
                        "Do not use star for fact memories."
                    ),
                },
            },
            "required": ["mem_ref"],
        },
    )
    def update(self, call: IntrospectionCall) -> IntrospectionResult:
        mem_ref = _read_mem_ref(call.args)
        if not mem_ref:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="mem_ref is required", llm_text="mem_ref is required")
        star, star_error = memory_star_from_args(call.args)
        if star_error:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text=star_error, llm_text=star_error)
        if star and mem_ref.startswith("fact:"):
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="star is only valid for case memories",
                llm_text="star is only valid for case memories",
            )
        payload_patch = dict(call.args.get("payload_patch") or {})
        if star:
            payload_patch.update(star)
        star_fields = star_text_fields(star) if star else {}
        provider = self.service.l3_selector.resolve()
        result = provider.correct(
            L3CorrectRequest(
                document_id=mem_ref,
                summary=str(call.args.get("summary")) if call.args.get("summary") is not None else None,
                search_text=str(call.args.get("search_text")) if call.args.get("search_text") is not None else None,
                payload_patch=payload_patch,
                topics=[str(value) for value in list(call.args.get("topics") or [])] if call.args.get("topics") is not None else None,
                situation_text=star_fields.get("situation_text") if star else None,
                task_text=star_fields.get("task_text") if star else None,
                action_text=star_fields.get("action_text") if star else None,
                result_text=star_fields.get("result_text") if star else None,
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
            "Forget an existing durable memory record from the active memory provider. "
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
