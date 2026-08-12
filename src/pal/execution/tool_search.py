from __future__ import annotations

from pal.shared.tool_protocol import new_tool_call

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import NextToolHint, ToolGuidance
from pal.execution.generated_tool_models import (
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinCapabilityCallInput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadInput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadOutput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinResultPageInput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinResultPageOutput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutput,
)
from pal.execution.tool_semantics import DIRECT_NONE
from uuid import uuid4
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
)
from pal.shared.result_rendering import render_titled_structured_for_llm


def inspect_tools(provider: object) -> list[dict[str, object]]:
    return provider.runtime.list_tool_specs()


class ExecutionToolSearchMixin:
    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="tools",
        guidance=ToolGuidance(
            purpose="List registered execution tools with descriptions and input schemas.",
            use_when="Browsing all available tools when you don't know what to search for. Checking tool inventory completeness.",
            do_not_use_when="Searching for a specific capability (use search_tools). Reading one tool's contract (use read_tool).",
            failure_next_steps="Read-only. If expected tools are missing, check exec_show for capability/tool counts.",
        ),
        aliases=("exec_tools",),
    )
    def tools(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = {"tools": inspect_tools(self)}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="execution tools",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Execution tools", payload),
        )


class ExecutionDiscoveryCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="exec",
        action_name="capability_call",
        guidance=ToolGuidance(
            purpose="Invoke one indirect capability by its exact alias.",
            use_when="After discovering the alias via search_tools/read_tool. Direct tools must be invoked directly.",
            do_not_use_when="For direct capabilities — they must be invoked directly and are rejected here.",
            failure_next_steps="Use read_tool to inspect the alias schema before retrying.",
        ),
        aliases=("call_tool",),
        InputModel=ExecutionToolSearchExecutionDiscoveryCapabilityMixinCapabilityCallInput,
        execution=DIRECT_NONE,
        metadata={"canonical_path": "op_tool_call"},
    )
    def capability_call(self, call: IntrospectionCall) -> IntrospectionResult:
        name = str(call.args.get("name") or "").strip()
        if not name:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="name is required",
                llm_text="name is required",
            )
        result = self.runtime.invoke_indirect_tool(
            new_tool_call(call_id=f"call_{uuid4().hex}", name=name, arguments=dict(call.args.get("args") or {}))
        )
        return _invocation_capability_result(self.runtime, name, result)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="discovery",
        action_name="search",
        guidance=ToolGuidance(
            purpose="Search execution capabilities by query text. Use namespace='inspect' for inspect/list/show, namespace='action' for mutate/execute/external.",
            use_when="When you need to find a capability by what it does but don't know its exact alias.",
            do_not_use_when="When you already know the alias (use read_tool or call_tool). When you want to invoke a known capability.",
            failure_next_steps="Try a different namespace, broader query, or set facets=true for narrowing stats.",
            next_tool_hints=(
                NextToolHint(
                    name="read_tool",
                    use_when="A search hit identifies the capability whose complete contract is needed.",
                ),
            ),
        ),
        aliases=("search_tools",),
        InputModel=ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput,
        OutputModel=ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutput,
        execution=DIRECT_NONE,
        metadata={"canonical_path": "op_tool_search"},
    )
    def search(self, call: IntrospectionCall) -> IntrospectionResult:
        search_args = dict(call.args)
        module_name = str(search_args.pop("module_name", "") or "").strip()
        if module_name:
            search_args["module_id"] = module_name
        payload = self.runtime._search_generation(self.runtime.registry_generation, search_args)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="capability search results",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Capability search results", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="discovery",
        action_name="read",
        guidance=ToolGuidance(
            purpose="Read the full capability contract for an execution capability by exact alias.",
            use_when="Before invoking an indirect capability via call_tool — inspect its input schema, output schema, and execution semantics.",
            do_not_use_when="Searching for capabilities by query (use search_tools). Listing all tools (use exec_tools).",
            failure_next_steps="If alias not found, use search_tools to discover the correct alias.",
            next_tool_hints=(
                NextToolHint(
                    name="call_tool",
                    use_when="The inspected capability is indirect and its validated arguments are ready.",
                ),
            ),
        ),
        aliases=("read_tool",),
        InputModel=ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadInput,
        OutputModel=ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadOutput,
        execution=DIRECT_NONE,
        metadata={"canonical_path": "op_tool_read"},
    )
    def read(self, call: IntrospectionCall) -> IntrospectionResult:
        alias = str(call.args.get("name") or "").strip()
        if not alias:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="name is required",
                structured={"reason": "name_missing"},
                llm_text="name is required; use search_tools to discover a current alias",
            )
        payload = self.runtime._read_generation_tool(self.runtime.registry_generation, alias)
        if payload is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="capability not found",
                structured={"reason": "capability_not_found"},
                llm_text="capability not found; use search_tools to discover a current alias",
            )
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="capability definition",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Capability definition", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="discovery",
        action_name="result_page",
        guidance=ToolGuidance(
            purpose="Read a page of a prior large tool result.",
            use_when="When a prior tool result was truncated or paginated. Use anchor='tail' for log-like output ends.",
            do_not_use_when="Not for new tool invocations.",
            failure_next_steps="Use the result_ref returned by the original paged result (normally its tool_call_id). For an unknown or expired handle, follow the returned recovery affordance: rerun only an idempotent read marked safe to retry; otherwise rediscover the origin with search_tools and reconcile its effect before any retry.",
        ),
        aliases=("read_tool_result",),
        InputModel=ExecutionToolSearchExecutionDiscoveryCapabilityMixinResultPageInput,
        OutputModel=ExecutionToolSearchExecutionDiscoveryCapabilityMixinResultPageOutput,
        execution=DIRECT_NONE,
        metadata={"canonical_path": "op_tool_result_page"},
    )
    def result_page(self, call: IntrospectionCall) -> CapabilityResult:
        page = self.runtime.read_tool_result_page(
            result_ref=str(call.args.get("result_ref") or ""),
            page=int(call.args.get("page") or 1),
            page_size=call.args.get("page_size"),
            anchor="tail" if bool(call.args.get("tail")) else str(call.args.get("anchor") or "head"),
            turn_id=str(call.meta.get("turn_id") or "") or None,
        )
        if page is None:
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text="tool result handle is unknown in this logical session",
                structured={"reason": "unknown_result_handle"},
                llm_text="tool result handle is unknown in this logical session",
            )
        if page.state != "ok":
            reason = (
                "expired_handle"
                if page.state == "expired_handle"
                else page.state
            )
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text=(
                    "tool result handle expired"
                    if reason == "expired_handle"
                    else "tool result page is unavailable"
                ),
                structured={
                    "reason": reason,
                    "result_ref": page.result_ref,
                    "origin": dict(page.origin),
                    "expires_at_user_turn": page.expires_at_user_turn,
                    "current_user_turn": page.current_user_turn,
                },
                llm_text=(
                    "tool result handle expired"
                    if reason == "expired_handle"
                    else "tool result page is unavailable"
                ),
            )
        payload = {
            "result_ref": page.result_ref,
            "page": page.page,
            "page_count": page.page_count,
            "has_more": page.has_more,
            "has_more_before": page.has_more_before,
            "has_more_after": page.has_more_after,
            "anchor": page.anchor,
            "anchor_page": page.anchor_page,
            "start_offset": page.start_offset,
            "end_offset": page.end_offset,
            "original_size": page.original_size,
            "page_size": page.page_size,
            "page_text": page.content,
        }
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=page.content,
            structured=payload,
            llm_text=page.content,
            context_delivery=(
                dict(page.context_delivery)
                if page.context_delivery
                else None
            ),
        )


def _invocation_capability_result(runtime: object, name: str, result: object) -> CapabilityResult:
    convert = getattr(runtime, "_canonical_result_from_invocation")
    canonical = convert(name, None, result)
    return CapabilityResult(
        status=canonical.status,
        text=canonical.text,
        structured=canonical.structured,
        llm_text=canonical.llm_text,
    )
