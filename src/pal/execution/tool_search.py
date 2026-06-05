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
)
from pal.shared.result_rendering import render_titled_structured_for_llm

_DESCRIPTION_PREVIEW_CHARS = 360
_EMPTY_QUERY_HIT_LIMIT = 25


def inspect_tools(provider) -> list[dict[str, object]]:
    return provider.runtime.list_tool_specs()


def _query_terms(query: str) -> list[str]:
    return [term for term in str(query).lower().split() if term]


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


def _read_name_arg(args: dict[str, object], *aliases: str) -> str:
    for key in aliases:
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _compact_description(value: object) -> str:
    text = " ".join(str(value or "").strip().split())
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
    return {
        "name": str(spec.get("canonical_path") or spec.get("name") or "").strip(),
        "description": _compact_description(spec.get("description")),
        "required_params": _required_params(spec),
    }


def _dedupe_hits(runtime: object, specs: list[dict[str, object]]) -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    by_name: dict[str, dict[str, object]] = {}
    for spec in specs:
        hit = _compact_capability_hit(runtime, spec)
        name = str(hit.get("name") or "").strip()
        if not name:
            continue
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = hit
            hits.append(hit)
    return hits


def _capability_facets(specs: list[dict[str, object]]) -> dict[str, object]:
    modules: dict[str, int] = {}
    families: dict[str, int] = {}
    for spec in specs:
        module_id = str(spec.get("module_id") or "").strip() or "unknown"
        family = str(spec.get("family") or "").strip() or "unknown"
        modules[module_id] = modules.get(module_id, 0) + 1
        families[family] = families.get(family, 0) + 1
    return {
        "modules": [{"module_id": key, "count": modules[key]} for key in sorted(modules)],
        "families": [{"family": key, "count": families[key]} for key in sorted(families)],
    }


def _required_params_from_schema(schema: object) -> list[str]:
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(item) for item in required if str(item).strip()]


def _llm_capability_contract(capability: dict[str, object]) -> dict[str, object]:
    parameters_schema = dict(capability.get("parameters_schema") or {"type": "object", "properties": {}})
    return {
        "name": str(capability.get("canonical_path") or capability.get("name") or "").strip(),
        "description": _compact_description(capability.get("description")),
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
    description: str = "Invoke a discovered capability by canonical path or alias."
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
        canonical_name = str((spec or {}).get("canonical_path") or (spec or {}).get("name") or target_name).strip()
        if spec is not None and canonical_name != self.name and canonical_name in getattr(self.runtime, "tools", {}):
            result = await self.runtime.execute_tool_async(
                CanonicalToolCall(name=canonical_name, args=capability_args),
                turn_id=str(kwargs.get("turn_id") or ""),
            )
            return CapabilityResult(
                status=result.status,
                text=result.text,
                structured=result.structured,
                llm_text=getattr(result, "llm_text", ""),
            )
        return await self.runtime.execute_async(CapabilityCall(name=target_name, args=capability_args, meta=meta))


class ExecutionDiscoveryCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="exec",
        action_name="capability_call",
        description="Invoke any registered capability by canonical path or alias. Use op_tool_search to find available capabilities first.",
        aliases=("capability_call",),
        args_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Canonical path or alias of the capability to invoke."},
                "args": {"type": "object", "description": "Arguments for the capability."},
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
        description="Search execution capabilities by query, module_id, or family. Empty calls return module/family counts only so the caller can narrow before listing.",
        aliases=("tool_search",),
        args_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "family": {"type": "string"},
                "module_id": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "top_k": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "description": "Alias for top_k."},
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
                "facets": {"type": "object"},
                "usage_hint": {"type": "string"},
            },
        },
        metadata={"canonical_path": "op_tool_search"},
    )
    def search(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "tool_search", call.args)

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
        return _tool_capability_result(self.runtime, "tool_read", call.args)


@dataclass
class ToolSearchTool:
    runtime: object
    name: str = "tool_search"
    display_name: str = "Tool Search"
    family: str = "discovery"
    description: str = "Search capability definitions by query, family, or module_id. Empty calls return module/family counts only; narrow by module_id before listing broad surfaces."
    tags: tuple[str, ...] = ("discovery", "search")
    keywords: tuple[str, ...] = ("find", "lookup", "discover", "tool")
    args_schema: dict[str, object] = None  # type: ignore[assignment]
    result_schema: dict[str, object] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "family": {"type": "string"},
                    "module_id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "top_k": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "description": "Alias for top_k."},
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
                    "facets": {"type": "object"},
                    "usage_hint": {"type": "string"},
                },
            }

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        query = str(args.get("query") or "").strip()
        family = str(args.get("family") or "").strip().lower()
        module_id = str(args.get("module_id") or "").strip().lower()
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
        filters_active = bool(query or family or module_id)
        if not filters_active:
            all_hits = _dedupe_hits(self.runtime, all_specs)
            hits = all_hits[:top_k] if len(all_hits) <= min(top_k, _EMPTY_QUERY_HIT_LIMIT) else []
            payload = {
                "hits": hits,
                "total_count": len(all_hits),
                "returned_count": len(hits),
                "top_k": top_k,
                "truncated": len(hits) < len(all_hits),
                "facets": _capability_facets(all_specs),
                "usage_hint": "Provide query, module_id, or family. For broad inventory, first inspect facets, then call tool_search with module_id.",
            }
            return CapabilityResult(
                status=RuntimeStatus.OK,
                text="capability search facets",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Capability search facets", payload),
            )

        ranked: list[tuple[int, dict[str, object]]] = []
        for spec in all_specs:
            if family and str(spec.get("family") or "").lower() != family:
                continue
            if module_id and str(spec.get("module_id") or "").lower() != module_id:
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
            ranked.append((score, spec))
        ranked.sort(
            key=lambda item: (
                -item[0],
                -_search_priority(item[1]),
                str(item[1].get("module_id") or ""),
                str(item[1].get("name") or ""),
            )
        )
        all_hits = _dedupe_hits(self.runtime, [spec for _, spec in ranked])
        hits = all_hits[:top_k]
        payload = {
            "hits": hits,
            "total_count": len(all_hits),
            "returned_count": len(hits),
            "top_k": top_k,
            "truncated": len(all_hits) > len(hits),
            "facets": _capability_facets([spec for _, spec in ranked]),
        }
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="capability search results",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Capability search results", payload),
        )


@dataclass
class ToolReadTool:
    runtime: object
    name: str = "tool_read"
    display_name: str = "Tool Read"
    family: str = "discovery"
    description: str = "Read the full definition for a capability by canonical path or alias."
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
        projected = _llm_capability_contract(capability)
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="capability definition",
            structured={"capability": projected},
            llm_text=render_titled_structured_for_llm("Capability definition", {"capability": projected}),
        )
