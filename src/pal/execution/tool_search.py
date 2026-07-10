from __future__ import annotations

from dataclasses import dataclass

from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.llm.contracts import CanonicalToolCall
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    llm_tool_name,
    replace_internal_tool_names,
    replace_internal_tool_names_in_value,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

_DESCRIPTION_PREVIEW_CHARS = 360


def inspect_tools(provider) -> list[dict[str, object]]:
    return provider.runtime.list_tool_specs()


def _query_terms(query: str) -> list[str]:
    return [term for term in str(query).lower().split() if term]


def _normalize_namespace(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"intro", "inspect", "inspection", INTROSPECTION_NAMESPACE}:
        return INTROSPECTION_NAMESPACE
    if text in {"op", "action", "actions", OPERATION_NAMESPACE}:
        return OPERATION_NAMESPACE
    return text


def _namespace_from_query(query: str) -> str:
    terms = set(_query_terms(query))
    if INTROSPECTION_NAMESPACE in terms or "intro" in terms or "inspect" in terms:
        return INTROSPECTION_NAMESPACE
    if OPERATION_NAMESPACE in terms or "op" in terms or "action" in terms:
        return OPERATION_NAMESPACE
    return ""


def _score(query: str, *fields: str) -> int:
    terms = _query_terms(query)
    if not terms:
        return 0
    score = 0
    for field in fields:
        text = str(field).strip().lower()
        if not text:
            continue
        if terms[0] in text:
            score += 5
        score += sum(1 for token in terms if token in text)
    return score


def _search_priority(spec: dict[str, object]) -> int:
    metadata = spec.get("metadata")
    if not isinstance(metadata, dict):
        return 0
    try:
        return int(metadata.get("search_priority") or 0)
    except (TypeError, ValueError):
        return 0


def _bool_arg(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _spec_namespace(spec: dict[str, object]) -> str:
    metadata = spec.get("metadata")
    if isinstance(metadata, dict):
        namespace = str(metadata.get("namespace") or "").strip().lower()
        if namespace:
            return namespace
    name = str(spec.get("canonical_path") or spec.get("name") or "").strip().lower()
    if name.startswith("intro_") or str(spec.get("family") or "").strip().lower() == "introspection":
        return INTROSPECTION_NAMESPACE
    if name.startswith("op_"):
        return OPERATION_NAMESPACE
    return ""


def _read_name_arg(args: dict[str, object], *aliases: str) -> str:
    for key in aliases:
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _compact_description(value: object) -> str:
    text = " ".join(replace_internal_tool_names(value).strip().split())
    if len(text) <= _DESCRIPTION_PREVIEW_CHARS:
        return text
    return f"{text[: _DESCRIPTION_PREVIEW_CHARS - 18].rstrip()} ... [truncated]"


def _required_params(spec: dict[str, object]) -> list[str]:
    schema = spec.get("parameters_schema")
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(item) for item in required if str(item).strip()]


def _compact_capability_hit(runtime: object, spec: dict[str, object]) -> dict[str, object]:
    _ = runtime
    canonical = str(spec.get("canonical_path") or spec.get("name") or "").strip()
    return {
        "name": llm_tool_name(canonical),
        "description": _compact_description(spec.get("description")),
        "required_params": _required_params(spec),
    }


def _dedupe_hits(runtime: object, specs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [_compact_capability_hit(runtime, spec) for spec in _dedupe_specs(specs)]


def _dedupe_specs(specs: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: list[dict[str, object]] = []
    by_name: dict[str, dict[str, object]] = {}
    for spec in specs:
        name = str(spec.get("canonical_path") or spec.get("name") or "").strip()
        if not name:
            continue
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = spec
            deduped.append(spec)
    return deduped


def _capability_facets(specs: list[dict[str, object]]) -> dict[str, object]:
    modules: dict[str, int] = {}
    families: dict[str, int] = {}
    namespaces: dict[str, int] = {}
    for spec in specs:
        namespace = _spec_namespace(spec) or "unknown"
        module_id = str(spec.get("module_id") or "").strip() or "unknown"
        family = str(spec.get("family") or "").strip() or "unknown"
        namespaces[namespace] = namespaces.get(namespace, 0) + 1
        modules[module_id] = modules.get(module_id, 0) + 1
        families[family] = families.get(family, 0) + 1
    return {
        "namespaces": [{"namespace": key, "count": namespaces[key]} for key in sorted(namespaces)],
        "modules": [{"module_id": key, "count": modules[key]} for key in sorted(modules)],
        "families": [{"family": key, "count": families[key]} for key in sorted(families)],
    }


def _applied_filters(*, query: str, namespace: str, family: str, module_id: str, tags: list[str]) -> dict[str, object]:
    filters: dict[str, object] = {}
    if query:
        filters["query"] = query
    if namespace:
        filters["namespace"] = namespace
    if family:
        filters["family"] = family
    if module_id:
        filters["module_id"] = module_id
    if tags:
        filters["tags"] = tags
    return filters


def _required_params_from_schema(schema: object) -> list[str]:
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(item) for item in required if str(item).strip()]


def _llm_capability_contract(capability: dict[str, object], *, full_description: bool = False) -> dict[str, object]:
    parameters_schema = replace_internal_tool_names_in_value(
        dict(capability.get("parameters_schema") or {"type": "object", "properties": {}})
    )
    canonical = str(capability.get("canonical_path") or capability.get("name") or "").strip()
    raw_description = replace_internal_tool_names(capability.get("description") or "")
    return {
        "name": llm_tool_name(canonical),
        "description": " ".join(raw_description.strip().split()) if full_description else _compact_description(raw_description),
        "parameters_schema": parameters_schema,
        "required_params": _required_params_from_schema(parameters_schema),
    }


class ExecutionToolSearchMixin:
    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="tools",
        description="List registered execution tools with descriptions and input schemas",
        aliases=("execution_tools",),
    )
    def tools(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="execution tools",
            structured={"tools": inspect_tools(self)},
            llm_text=render_titled_structured_for_llm("Execution tools", {"tools": inspect_tools(self)}),
        )


def _tool_capability_result(runtime, tool_name: str, args: dict[str, object]) -> IntrospectionResult:
    result = runtime.execute_tool(type("ToolCall", (), {"name": tool_name, "args": dict(args)})())
    return IntrospectionResult(
        status=RuntimeStatus.OK if result.ok else RuntimeStatus.ERROR,
        text=result.text,
        structured=result.structured,
        llm_text=result.llm_text,
    )


@dataclass
class ToolCallTool:
    runtime: object
    name: str = "op_tool_call"
    display_name: str = "Tool Call"
    family: str = "discovery"
    description: str = "Invoke a discovered capability by its tool name or alias."
    tags: tuple[str, ...] = ("discovery", "capability", "invoke")
    keywords: tuple[str, ...] = ("call", "invoke", "capability", "tool")
    args_schema: dict[str, object] = None  # type: ignore[assignment]
    result_schema: dict[str, object] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Canonical path or alias of the capability to invoke."},
                    "args": {"type": "object", "description": "Arguments for the capability."},
                },
                "required": ["name"],
            }
        if self.result_schema is None:
            self.result_schema = {"type": "object", "properties": {}}

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        target_name = _read_name_arg(args, "name", "capability", "tool")
        if not target_name:
            return CapabilityResult(status=RuntimeStatus.INVALID, text="name is required", llm_text="name is required")
        capability_args = dict(args.get("args") or {})
        return self.runtime.execute(CapabilityCall(name=target_name, args=capability_args))

    async def ainvoke(self, args: dict[str, object], **kwargs: object) -> CapabilityResult:
        target_name = _read_name_arg(args, "name", "capability", "tool")
        if not target_name:
            return CapabilityResult(status=RuntimeStatus.INVALID, text="name is required", llm_text="name is required")
        capability_args = dict(args.get("args") or {})
        turn_id = str(kwargs.get("turn_id") or "").strip()
        meta = {"turn_id": turn_id} if turn_id else {}
        spec = self.runtime.get_capability_spec(target_name)
        resolve_tool_name = getattr(self.runtime, "resolve_llm_tool_name", None)
        resolved_tool_name = str(resolve_tool_name(target_name) if callable(resolve_tool_name) else target_name).strip()
        canonical_name = str((spec or {}).get("canonical_path") or (spec or {}).get("name") or resolved_tool_name or target_name).strip()
        execute_tool_async = getattr(self.runtime, "execute_tool_async", None)
        if callable(execute_tool_async) and canonical_name != self.name:
            result = await execute_tool_async(
                CanonicalToolCall(name=canonical_name, args=capability_args),
                allow_tools=bool(kwargs.get("allow_tools", True)),
                budget=kwargs.get("budget"),
                turn_id=str(kwargs.get("turn_id") or ""),
            )
            return CapabilityResult(
                status=result.status,
                text=result.text,
                structured=result.structured,
                llm_text=getattr(result, "llm_text", ""),
            )
        execute_async = getattr(self.runtime, "execute_async", None)
        if callable(execute_async):
            return await execute_async(CapabilityCall(name=target_name, args=capability_args, meta=meta))
        return CapabilityResult(
            status=RuntimeStatus.NOT_FOUND,
            text=f"unknown capability: {target_name}",
            structured={"reason": "unknown_capability", "capability": target_name},
            llm_text=f"unknown capability: {target_name}",
        )


class ExecutionDiscoveryCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="exec",
        action_name="capability_call",
        description=(
            "Invoke any registered capability by exact tool name or alias. Resident tools are the fast path for common "
            "capabilities already exposed directly to the model; call_tool is the generic execution path for the broader "
            "capability surface. Use search_tools/read_tool when you need to discover a capability name or inspect its "
            "argument schema; do not guess hidden capability names."
        ),
        aliases=("capability_call",),
        args_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact registered capability name or alias. Use search_tools/read_tool first when unsure.",
                },
                "args": {
                    "type": "object",
                    "description": "Arguments matching that capability's schema from read_tool.",
                },
            },
            "required": ["name"],
        },
        metadata={"canonical_path": "op_tool_call"},
    )
    def capability_call(self, call: IntrospectionCall) -> IntrospectionResult:
        from pal.execution.contracts import CapabilityCall

        name = str(call.args.get("name") or "").strip()
        if not name:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="name is required")
        capability_args = dict(call.args.get("args") or {})
        result = self.runtime.execute(CapabilityCall(name=name, args=capability_args, meta=dict(call.meta)))
        return IntrospectionResult(
            status=result.status,
            text=result.text,
            structured=result.structured,
            llm_text=getattr(result, "llm_text", ""),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="discovery",
        action_name="search",
        description=(
            "Search execution capabilities by query text. Use namespace='inspect' for inspect/list/show capabilities "
            "and namespace='action' for capabilities that mutate, execute, or call external services. Set facets=true only for broad searches that need narrowing statistics."
        ),
        aliases=("tool_search",),
        args_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search text or partial capability name, for example 'llm endpoint config' or 'send attachment'.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Capability namespace. Use inspect to inspect state; use action to perform work.",
                    "enum": ["inspect", "action", "introspection", "operation"],
                },
                "family": {"type": "string", "description": "Optional family filter such as management, lifecycle, endpoint, or search."},
                "module_id": {"type": "string", "description": "Optional module filter such as llm, memory, channel, artifact, minion, or web_search."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags that every result must include."},
                "top_k": {"type": "integer", "minimum": 1, "description": "Maximum number of compact hits to return."},
                "limit": {"type": "integer", "minimum": 1, "description": "Alias for top_k."},
                "facets": {"type": "boolean", "description": "Default false. Set true to include namespace/module/family counts for broad-search narrowing."},
            },
        },
        result_schema={
            "type": "object",
            "properties": {
                "hits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "required_params": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "description", "required_params"],
                    },
                },
                "total_count": {"type": "integer"},
                "returned_count": {"type": "integer"},
                "top_k": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "applied_filters": {"type": "object"},
                "facets": {"type": "object", "description": "Only present when requested with facets=true; counts deduplicated candidates."},
                "usage_hint": {"type": "string", "description": "Only present for broad facet responses that need narrowing guidance."},
            },
        },
        metadata={"canonical_path": "op_tool_search"},
    )
    def search(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_tool_search", call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="discovery",
        action_name="read",
        description="Read the full capability contract for an execution capability by exact name.",
        aliases=("tool_read",),
        args_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        result_schema={"type": "object", "properties": {"capability": {"type": "object"}}},
        metadata={"canonical_path": "op_tool_read"},
    )
    def read(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_tool_read", call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="discovery",
        action_name="result_page",
        description=(
            "Read a page of a prior large tool result. Use anchor='head' for normal forward pages or anchor='tail' "
            "to inspect the newest/end of log-like output. Pass the original tool_call_id as result_ref."
        ),
        aliases=("tool_result_page",),
        args_schema={
            "type": "object",
            "properties": {
                "result_ref": {
                    "type": "string",
                    "description": "The result_ref shown in a prior tool result; this is the original tool_call_id.",
                },
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "1-based page number. With anchor='head', page=1 is the first page. "
                        "With anchor='tail', page=1 is the last page and page=2 is second-to-last."
                    ),
                },
                "anchor": {
                    "type": "string",
                    "enum": ["head", "tail"],
                    "description": "Read from the start ('head') or end ('tail') of the paged result. Defaults to head.",
                },
                "tail": {"type": "boolean", "description": "Shorthand for anchor='tail'. Useful for log-like output."},
                "page_size": {"type": "integer", "minimum": 256, "description": "Optional character page size."},
            },
            "required": ["result_ref"],
        },
        result_schema={
            "type": "object",
            "properties": {
                "result_ref": {"type": "string"},
                "page": {"type": "integer"},
                "page_count": {"type": "integer"},
                "has_more": {"type": "boolean"},
                "has_more_before": {"type": "boolean"},
                "has_more_after": {"type": "boolean"},
                "anchor": {"type": "string"},
                "anchor_page": {"type": "integer"},
            },
        },
        metadata={"canonical_path": "op_tool_result_page"},
    )
    def result_page(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_tool_result_page", call.args)


@dataclass
class ToolSearchTool:
    runtime: object
    name: str = "op_tool_search"
    display_name: str = "Tool Search"
    family: str = "discovery"
    description: str = (
        "Search capability definitions by query text. Use namespace='inspect' for inspect/list/show capabilities "
        "and namespace='action' for actions that mutate, execute, or call external services. Set facets=true only for broad searches that need narrowing statistics."
    )
    tags: tuple[str, ...] = ("discovery", "search")
    keywords: tuple[str, ...] = ("find", "lookup", "discover", "tool")
    args_schema: dict[str, object] = None  # type: ignore[assignment]
    result_schema: dict[str, object] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search text or partial capability name, for example 'llm endpoint config' or 'send attachment'.",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Capability namespace. Use inspect to inspect state; use action to perform work.",
                        "enum": ["inspect", "action", "introspection", "operation"],
                    },
                    "family": {"type": "string", "description": "Optional family filter such as management, lifecycle, endpoint, or search."},
                    "module_id": {"type": "string", "description": "Optional module filter such as llm, memory, channel, artifact, minion, or web_search."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags that every result must include."},
                    "top_k": {"type": "integer", "minimum": 1, "description": "Maximum number of compact hits to return."},
                    "limit": {"type": "integer", "minimum": 1, "description": "Alias for top_k."},
                    "facets": {"type": "boolean", "description": "Default false. Set true to include namespace/module/family counts for broad-search narrowing."},
                },
            }
        if self.result_schema is None:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "hits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "required_params": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["name", "description", "required_params"],
                        },
                    },
                    "total_count": {"type": "integer"},
                    "returned_count": {"type": "integer"},
                    "top_k": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "applied_filters": {"type": "object"},
                    "facets": {"type": "object", "description": "Only present when requested with facets=true; counts deduplicated candidates."},
                    "usage_hint": {"type": "string", "description": "Only present for broad facet responses that need narrowing guidance."},
                },
            }

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        query = _read_name_arg(args, "query", "name")
        namespace = _normalize_namespace(args.get("namespace")) or _namespace_from_query(query)
        family = str(args.get("family") or "").strip().lower()
        module_id = str(args.get("module_id") or "").strip().lower()
        tags = [
            str(item).strip().lower()
            for item in list(args.get("tags") or [])
            if str(item).strip()
        ]
        include_facets = _bool_arg(args.get("facets") if "facets" in args else args.get("include_facets"))
        try:
            top_k = max(1, int(args.get("top_k", args.get("limit", 10))))
        except (TypeError, ValueError):
            top_k = 10

        all_specs = [
            spec
            for spec in self.runtime.list_capability_specs()
            if not (
                str(spec["name"]).startswith("introspection_")
                and "execution tools" in str(spec.get("description") or "").lower()
            )
        ]

        ranked: list[tuple[int, int, dict[str, object]]] = []
        for index, spec in enumerate(all_specs):
            if namespace and _spec_namespace(spec) != namespace:
                continue
            if family and str(spec.get("family") or "").lower() != family:
                continue
            if module_id and str(spec.get("module_id") or "").lower() != module_id:
                continue
            if tags:
                spec_tags = {str(item).strip().lower() for item in list(spec.get("tags") or []) if str(item).strip()}
                if not set(tags).issubset(spec_tags):
                    continue
            aliases = list(spec.get("aliases") or [])
            call_names = list(spec.get("call_names") or [])
            score = _score(
                query,
                str(spec.get("canonical_path") or ""),
                str(spec.get("name") or ""),
                str(spec.get("display_name") or ""),
                str(spec.get("description") or ""),
                str(spec.get("family") or ""),
                str(spec.get("module_id") or ""),
                " ".join(sorted(str(item) for item in aliases)),
                " ".join(sorted(str(item) for item in call_names)),
            )
            if query and score <= 0:
                continue
            ranked.append((score, index, spec))
        ranked.sort(
            key=lambda item: (
                -item[0],
                -_search_priority(item[2]),
                str(item[2].get("module_id") or "") if query else "",
                str(item[2].get("name") or "") if query else "",
                item[1],
            )
        )
        deduped_specs = _dedupe_specs([spec for _, _, spec in ranked])
        all_hits = [_compact_capability_hit(self.runtime, spec) for spec in deduped_specs]
        hits = all_hits[:top_k]
        payload = {
            "hits": hits,
            "total_count": len(all_hits),
            "returned_count": len(hits),
            "top_k": top_k,
            "truncated": len(all_hits) > len(hits),
            "applied_filters": _applied_filters(
                query=query,
                namespace=namespace,
                family=family,
                module_id=module_id,
                tags=tags,
            ),
        }
        if include_facets:
            payload["facets"] = _capability_facets(deduped_specs)
            if len(all_hits) > len(hits):
                payload["usage_hint"] = (
                    "Narrow with namespace, module_id, family, or tags; facets count the deduplicated candidate set."
                )
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="capability search results",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Capability search results", payload),
        )


@dataclass
class ToolReadTool:
    runtime: object
    name: str = "op_tool_read"
    display_name: str = "Tool Read"
    family: str = "discovery"
    description: str = "Read the full definition for a capability by tool name or alias."
    tags: tuple[str, ...] = ("discovery", "read")
    keywords: tuple[str, ...] = ("inspect", "definition", "schema", "tool")
    args_schema: dict[str, object] = None  # type: ignore[assignment]
    result_schema: dict[str, object] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            }
        if self.result_schema is None:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "capability": {"type": "object"},
                },
            }

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        name = _read_name_arg(args, "name", "tool_name", "tool", "id", "target", "query")
        if not name:
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="name missing",
                structured={"reason": "name_missing"},
                llm_text="name missing",
            )
        capability = self.runtime.get_capability_spec(name)
        if capability is None:
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text="capability not found",
                structured={"reason": "capability_not_found"},
                llm_text="capability not found",
            )
        projected = _llm_capability_contract(capability, full_description=True)
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="capability definition",
            structured={"capability": projected},
            llm_text=render_titled_structured_for_llm("Capability definition", {"capability": projected}),
        )
