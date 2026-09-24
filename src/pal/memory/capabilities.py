from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_LOCAL_READ,
    DIRECT_LOCAL_WRITE,
    INDIRECT_EXTERNAL_WRITE,
    INDIRECT_LOCAL_WRITE,
)
from pal.execution.tool_facade import NextToolHint, ToolGuidance

from pal.execution.generated_tool_models import (
    MemoryCapabilitiesMemoryIntrospectionProviderDeleteInput,
    MemoryCapabilitiesMemoryIntrospectionProviderRecallInput,
    MemoryCapabilitiesMemoryIntrospectionProviderSetActiveProviderInput,
    MemoryCapabilitiesMemoryIntrospectionProviderUpdateInput,
    MemoryCapabilitiesMemoryIntrospectionProviderWriteInput,
)
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pal.control.contracts import ControlAction, InteractionMessageSpec
from pal.control.interactions import delivery_for_interaction
from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.contracts import CapabilityCall
from pal.memory.mutations import mutation_id_from_call
from pal.memory.review_models import CommitMemoryCandidatesInput
from pal.memory.dreaming.tool_models import DreamingInput, MemoryHistoryInput
from pal.memory.candidates import memory_star_from_args, star_text_fields
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

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="dreaming",
        InputModel=DreamingInput, execution=INDIRECT_LOCAL_WRITE, aliases=("memory_dreaming",),
        async_handler_name="dreaming_async",
        examples=({"operation": "status"}, {"operation": "start", "dry_run": True}),
        guidance=ToolGuidance(purpose="Inspect, configure, enable automatic scheduling, start or resume memory duplicate consolidation.",
            use_when="The user requests dreaming, a dry run, its status/report, or changes to automatic scheduling and configuration.",
            do_not_use_when="For immediate memory corrections, use update.",
            failure_next_steps="Read the run report; failed runs preserve the published generation."))
    def dreaming(self, call: IntrospectionCall) -> IntrospectionResult:
        service = self.context.port_registry.get("memory.dreaming:dreaming")
        if service is None:
            return IntrospectionResult(status="unavailable", text="Dreaming is only available on the main Pal runtime.")
        operation = str(call.args.get("operation") or "status")
        run_id = call.args.get("run_id")
        if operation in {"configure", "enable", "disable"}:
            try:
                changes = call.args.get("config") if operation == "configure" else {"enabled": operation == "enable"}
                result = service.configure(changes)
            except (ValueError, TypeError) as exc:
                return IntrospectionResult(status="invalid", text=f"Invalid dreaming configuration: {exc}")
        elif operation == "config":
            result = service.status()["current_configuration"]
        elif operation in {"status", "report"}:
            result = service.status(run_id)
        elif operation == "start":
            result = service.start(dry_run=bool(call.args.get("dry_run")))
        elif operation == "resume":
            result = service.start(resume=run_id or service.status().get("run_id"))
        else:
            return IntrospectionResult(status="invalid", text="Unsupported dreaming operation.")
        return IntrospectionResult(status="ok", text="Dreaming", structured=result,
            llm_text=render_titled_structured_for_llm("Dreaming", result))

    async def dreaming_async(self, call: IntrospectionCall) -> IntrospectionResult:
        return self.dreaming(call)

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="history",
        InputModel=MemoryHistoryInput, aliases=("memory_history",),
        execution=INDIRECT_LOCAL_READ,
        examples=({"query": "prior API preference"},),
        guidance=ToolGuidance(purpose="Explicitly read archived original memories and successor references.",
            use_when="Current recall lacks historical details or a memory reference has been replaced.",
            do_not_use_when="Do not treat historical results as current facts or install them into L2 HOT.",
            failure_next_steps="Try the current successor reference or a more specific archive query."))
    def history(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self.service._resolve_l3_provider()
        repo = getattr(provider, "repository", None)
        storage = getattr(repo, "catalog", None)
        if storage is None:
            return IntrospectionResult(status="unavailable", text="No memory archive is available.")
        ref = str(call.args.get("mem_ref") or "")
        query = str(call.args.get("query") or "")
        results = storage.history(ref, repo=repo) if ref else storage.search_archive(query, limit=int(call.args.get("limit") or 8), repo=repo)
        payload = {"historical": True, "items": results}
        return IntrospectionResult(status="ok", text="Historical memory originals", structured=payload,
            llm_text=render_titled_structured_for_llm("Historical memory originals", payload))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="commit_candidates",
        InputModel=CommitMemoryCandidatesInput, aliases=("commit_memory_candidates",),
        execution=INDIRECT_EXTERNAL_WRITE,
        guidance=ToolGuidance(purpose="Commit a memory batch already authorized by the user's final review action.",
            use_when="Retrying a previously authorized memory candidate batch.",
            do_not_use_when="Candidates have not received the user's final batch approval. Use the review UI.",
            failure_next_steps="Open /memory_review with the batch ID. An unavailable provider leaves the draft intact."))
    def commit_candidates(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            manager = self.context.port_registry.get("bunshin:bunshin")
            result = self.service.reviews.commit(str(call.args.get("batch_id") or ""),
                validate_source=getattr(manager, "validate_memory_proposal_source", None))
        except ValueError as exc:
            return IntrospectionResult(status="invalid", text=str(exc), llm_text=str(exc))
        return IntrospectionResult(status=result["status"], text="Memory review batch commit", structured=result,
            llm_text=render_titled_structured_for_llm("Memory review batch commit", result))

    async def handle_memory_review_open_async(self, action: ControlAction):
        try:
            state = self.service.reviews.get(str(action.args.get("batch_id") or ""), action.route, resume=True)
            return {"delivery": self.service.reviews.delivery(state, action.route, opening=True)}
        except ValueError as exc:
            return {"message": str(exc)}

    async def handle_memory_candidate_decision_async(self, action: ControlAction):
        reviews = self.service.reviews
        # Old provider keyboards carried the entire proposal. Import it for review;
        # their former all-at-once Accept must never grant new write authority.
        if "memory_candidates" in action.args:
            state = reviews.stage_payload({**action.args, "candidate_batch_id": action.target_id}, action.route, legacy=True)
            core = self.context.port_registry.get("core:core")
            if core is not None:
                old = InteractionMessageSpec(f"memory_candidate_{action.target_id}",
                    "memory_candidate_approval", action.route, "The legacy proposal has moved to per-candidate review.")
                await core.handle_control_action_async(ControlAction("interactive_resolve", "interaction",
                    target_id=old.interaction_id, route=action.route,
                    delivery=delivery_for_interaction(action.route, "interactive_resolve", old)))
            return {"delivery": reviews.delivery(state, action.route, opening=True,
                banner="Legacy proposal imported. Review each candidate, then submit the batch.")}
        try:
            state = reviews.apply(action.target_id, action.args, action.route)
            banner = ""
            if state["status"] == "authorized" and action.args.get("decision") in {"submit", "retry"}:
                result = await self.context.execution_runtime.execute_async(CapabilityCall(
                    name="op_memory_commit_candidates", args={"batch_id": state["batch_id"]}))
                if str(getattr(result, "status", "")) != "ok":
                    banner = "Submission did not complete. Your review decisions are saved; you can retry."
                state = reviews.get(state["batch_id"], action.route)
            operation = action.args.get("decision")
            view = "view" if operation == "save" else operation if operation in {"edit", "field", "view"} else "overview"
            return {"delivery": reviews.delivery(state, action.route, view=view,
                candidate_id=action.args.get("candidate_id", ""), field=action.args.get("field", ""), banner=banner)}
        except ValueError as exc:
            try:
                state = reviews.get(action.target_id, action.route)
                return {"delivery": reviews.delivery(state, action.route, banner=str(exc))}
            except ValueError:
                return {"message": str(exc)}

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show",
        guidance=ToolGuidance(
            purpose="Show memory runtime state.",
            use_when="Diagnosing memory system health — provider count, active provider, record counts.",
            do_not_use_when="Recalling specific memories (use recall_memory).",
            failure_next_steps="Read-only diagnostic. If no active provider, check memory_list_providers.",
        ), aliases=("memory_show",))
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
        guidance=ToolGuidance(
            purpose="List registered L3 memory providers.",
            use_when="Checking which memory backends are installed and mounted.",
            do_not_use_when="Checking the active provider (use memory_active_provider). Recalling memories (use recall_memory).",
            failure_next_steps="Read-only. If empty, no L3 plugins are installed.",
        ),
        aliases=("memory_list_providers",),
    )
    def list_providers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = []
        for provider_id in sorted(self.context.execution_runtime.l3_plugin_registry.plugins):
            provider = self.context.execution_runtime.l3_plugin_registry.get(provider_id)
            items.append(
                {
                    "name": provider_id,
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
        guidance=ToolGuidance(
            purpose="Show the current active memory provider.",
            use_when="Checking which backend handles recall/remember/update/forget operations.",
            do_not_use_when="Listing all providers (use memory_list_providers). Switching providers (use memory_set_active_provider).",
            failure_next_steps="Read-only. If none active, memories cannot be stored or recalled.",
        ),
        aliases=("memory_active_provider",),
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
        guidance=ToolGuidance(
            purpose="Recall durable memory records from the active memory provider.",
            use_when=(
                "The task depends on durable facts or past experience missing from the current context. "
                "For recurring failures or past repair decisions, use kind='case' with concrete error, symptom or fix terms."
            ),
            do_not_use_when=(
                "Current context already provides sufficient evidence, including a first error with a clear recovery. "
                "Current runtime state needs live introspection; current external facts need external verification."
            ),
            failure_next_steps=(
                "Use targeted queries with limit 3-5. "
                "mem_ref is opaque; copy exactly including prefixes like fact: or case:."
            ),
            next_tool_hints=(
                NextToolHint(
                    name="remember_memory",
                    use_when="No recalled record covers the explicit durable fact, preference, or reusable case.",
                ),
                NextToolHint(
                    name="update_memory",
                    use_when="The user explicitly corrects a known memory; semantic consolidation belongs to dreaming.",
                ),
                NextToolHint(
                    name="forget_memory",
                    use_when="The user explicitly requests deletion of a recalled memory record.",
                ),
            ),
        ),
        metadata={"omit_family_in_canonical": True},
        InputModel=MemoryCapabilitiesMemoryIntrospectionProviderRecallInput,
        execution=DIRECT_LOCAL_WRITE,
        aliases=("recall_memory",),
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
        guidance=ToolGuidance(
            purpose="Remember a new durable memory record.",
            use_when=(
                "Only when the user explicitly asks to remember/save, or states a clear durable fact/preference. "
                "For facts, preferences, project context, prior decisions. "
                "After fixing a bug or completing a debugging session with reusable lessons — write kind='case'. "
                "Remember creates a new record; semantic duplicates are handled by dreaming. "
                "Use update_memory only for an explicit correction to a known record, never for speculative consolidation."
            ),
            do_not_use_when=(
                "Not for current runtime state or external facts. "
                "Do not invent mem_ref values. Canonical conflicts require explicit update."
            ),
            failure_next_steps=(
                "Use summary for prompt-ready text and search_text for retrieval; mem_ref prefixes (fact:/case:) are "
                "part of the ref. If creation may have succeeded, reconcile with recall_memory using the candidate "
                "text before retrying so a duplicate record is not created."
            ),
        ),
        metadata={"omit_family_in_canonical": True},
        InputModel=MemoryCapabilitiesMemoryIntrospectionProviderWriteInput,
        execution=INDIRECT_EXTERNAL_WRITE,
        aliases=("remember_memory",),
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
        if kind == "case" and call.args.get("source_event_id"):
            payload["source_event_id"] = str(call.args["source_event_id"])
        if star:
            payload.update(star)
        star_fields = star_text_fields(star) if star else {}
        provider = self.service.l3_selector.resolve()
        result = provider.commit(
            L3CommitRequest(
                mutation_id=mutation_id_from_call(call),
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
        guidance=ToolGuidance(
            purpose="Update an existing durable memory record.",
            use_when=(
                "Instead of remember_memory when a recalled record is being corrected, superseded, or already covers the candidate. "
                "Copy mem_ref exactly from recall_memory results, including prefixes like fact: or case:."
            ),
            do_not_use_when=(
                "Not for new memories (use remember_memory). "
                "Do not invent or shorten mem_ref values."
            ),
            failure_next_steps="Correct invalid input and copy mem_ref exactly from recall_memory. If the update outcome is uncertain, recall that mem_ref and reconcile its current content before following any retry affordance.",
        ),
        metadata={"omit_family_in_canonical": True},
        InputModel=MemoryCapabilitiesMemoryIntrospectionProviderUpdateInput,
        execution=INDIRECT_EXTERNAL_WRITE,
        aliases=("update_memory",),
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
                mutation_id=mutation_id_from_call(call),
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
        guidance=ToolGuidance(
            purpose="Forget an existing durable memory record.",
            use_when=(
                "Only when the user explicitly asks to forget/delete a specific memory, or approves deleting a clearly invalid record. "
                "Copy mem_ref exactly from recall_memory results, including prefixes like fact: or case:."
            ),
            do_not_use_when=(
                "Do not invent or shorten mem_ref values."
            ),
            failure_next_steps="Correct invalid input and copy mem_ref exactly from recall_memory. If deletion may have succeeded, reconcile by recalling that mem_ref before following any retry affordance; do not issue a blind duplicate delete.",
        ),
        metadata={"omit_family_in_canonical": True},
        InputModel=MemoryCapabilitiesMemoryIntrospectionProviderDeleteInput,
        execution=INDIRECT_EXTERNAL_WRITE,
        aliases=("forget_memory",),
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
        guidance=ToolGuidance(
            purpose="Switch the active L3 memory provider.",
            use_when="Changing which memory backend handles recall/remember/update/forget.",
            do_not_use_when="Checking the active provider (use memory_active_provider). Listing providers (use memory_list_providers).",
            failure_next_steps="If the provider name is not found, verify it with memory_list_providers.",
        ),
        InputModel=MemoryCapabilitiesMemoryIntrospectionProviderSetActiveProviderInput,
        aliases=("memory_set_active_provider",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_active_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        provider_id = str(call.args.get("name") or "").strip()
        if not provider_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="name is required",
                llm_text="name is required",
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


def register_with_core(
    context: MainContext,
    service: MemoryService,
    *,
    config: Any = None,
) -> ModuleHandle:
    _ = config
    from pal.memory.prompt import MemoryPromptFragmentProvider
    from pal.memory.runtime_state import MemoryRuntimeStatePort

    provider = MemoryIntrospectionProvider(service=service, context=context)
    prompt_provider = MemoryPromptFragmentProvider()
    handle = ModuleHandle(
        module_id="memory",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        control_action_handlers={
            "memory_candidate_decision": provider.handle_memory_candidate_decision_async,
            "memory_review_open": provider.handle_memory_review_open_async,
        },
        ports={"memory": service},
        runtime_state_port=MemoryRuntimeStatePort(service),
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]
