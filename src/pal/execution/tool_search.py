from __future__ import annotations

from pal.shared.tool_protocol import new_tool_call

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import NextToolHint, ToolGuidance
from pal.execution.generated_tool_models import (
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinCapabilityCallInput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadInput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadOutput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput,
    ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutput,
)
from pal.execution.tool_semantics import DIRECT_NONE
from pal.execution.tool_presentation import render_tool_definition, render_tool_inventory, render_tool_search
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
            llm_text=render_tool_inventory(payload),
        )


class ExecutionDiscoveryCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="exec",
        action_name="capability_call",
        guidance=ToolGuidance(
            purpose="Invoke one indirect capability by its exact alias.",
            use_when="When the exact alias and sufficient valid contract are already known. Discover or inspect only missing information. Direct tools must be invoked directly.",
            do_not_use_when="For direct capabilities — they must be invoked directly and are rejected here.",
            failure_next_steps="Follow the returned error and recovery affordance. Read the contract only for missing or changed argument/execution semantics; discovery cannot repair an offline target or failed command.",
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
            purpose="Search capabilities by task or alias. Start with query and optionally module_name; add namespace/family filters only using values observed in hits or facets.",
            use_when="When you need to find a capability by what it does but don't know its exact alias.",
            do_not_use_when="When you already know the alias (use read_tool or call_tool). When you want to invoke a known capability.",
            failure_next_steps="For empty results, use filter_suggestions or remove guessed filters. Use facets=true to discover classifications before narrowing; family is not a business-domain taxonomy.",
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
            llm_text=render_tool_search(self.runtime.registry_generation, payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="discovery",
        action_name="read",
        guidance=ToolGuidance(
            purpose="Read the full capability contract for an execution capability by exact alias.",
            use_when="When the capability contract is absent, changed, or insufficient for correct arguments and execution semantics. Reuse a valid contract already in context; do not read it before every call.",
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
            llm_text=render_tool_definition(payload),
        )



def _invocation_capability_result(runtime: object, name: str, result: object) -> CapabilityResult:
    convert = getattr(runtime, "_canonical_result_from_invocation")
    canonical = convert(name, None, result)
    return CapabilityResult(
        status=canonical.status,
        text=canonical.text,
        structured=canonical.structured,
        llm_text=canonical.llm_text,
        snapshot_refs=canonical.snapshot_refs,
    )
