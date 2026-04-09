from __future__ import annotations

from dataclasses import dataclass

from pal.execution.contracts import CapabilityResult
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
)


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


def _read_name_arg(args: dict[str, object], *aliases: str) -> str:
    for key in aliases:
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _compact_capability_hit(spec: dict[str, object]) -> dict[str, object]:
    return {
        "name": spec["name"],
        "display_name": spec.get("display_name"),
        "description": spec.get("description"),
        "family": spec.get("family"),
        "module_id": spec.get("module_id"),
        "aliases": sorted(spec.get("aliases") or []),
    }


class ExecutionToolSearchMixin:
    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="tools",
        description="List registered execution tools with descriptions and input schemas",
        aliases=("execution.tools",),
    )
    def tools(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="execution tools",
            structured={"tools": inspect_tools(self)},
        )


def _tool_capability_result(runtime, tool_name: str, args: dict[str, object]) -> IntrospectionResult:
    result = runtime.execute_tool(type("ToolCall", (), {"name": tool_name, "args": dict(args)})())
    return IntrospectionResult(
        status=RuntimeStatus.OK if result.ok else RuntimeStatus.ERROR,
        text=result.text,
        structured=result.structured,
    )


class ExecutionDiscoveryCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="discovery",
        action_name="search",
        description="Search execution capabilities by name, family, tags, keywords, or description.",
        aliases=("tool.search",),
        args_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "family": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "top_k": {"type": "integer", "minimum": 1},
            },
        },
        result_schema={"type": "object", "properties": {"hits": {"type": "array", "items": {"type": "object"}}}},
        metadata={"llm_exposed": True},
    )
    def search(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "tool.search", call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="discovery",
        action_name="read",
        description="Read the full capability contract for an execution capability by exact name.",
        aliases=("tool.read",),
        args_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        result_schema={"type": "object", "properties": {"capability": {"type": "object"}}},
        metadata={"llm_exposed": True},
    )
    def read(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "tool.read", call.args)


@dataclass
class ToolSearchTool:
    runtime: object
    name: str = "tool.search"
    display_name: str = "Tool Search"
    family: str = "discovery"
    description: str = "Search capability definitions by name, family, aliases, module, or description. Use query for natural language discovery."
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
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "top_k": {"type": "integer", "minimum": 1},
                },
            }
        if self.result_schema is None:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "hits": {"type": "array", "items": {"type": "object"}},
                },
            }

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        query = str(args.get("query") or "").strip()
        family = str(args.get("family") or "").strip().lower()
        try:
            top_k = max(1, int(args.get("top_k", 5)))
        except (TypeError, ValueError):
            top_k = 5

        ranked: list[tuple[int, dict[str, object]]] = []
        for spec in self.runtime.list_capability_specs():
            if str(spec["name"]).startswith("introspection.") and "execution tools" in str(spec.get("description") or "").lower():
                continue
            if family and str(spec.get("family") or "").lower() != family:
                continue
            aliases = list(spec.get("aliases") or [])
            score = _score(
                query,
                str(spec.get("name") or ""),
                str(spec.get("display_name") or ""),
                str(spec.get("description") or ""),
                str(spec.get("family") or ""),
                str(spec.get("module_id") or ""),
                " ".join(sorted(str(item) for item in aliases)),
            )
            if query and score <= 0:
                continue
            ranked.append((score, spec))
        ranked.sort(key=lambda item: (-item[0], str(item[1].get("name") or "")))
        hits = [_compact_capability_hit(spec) for _, spec in ranked[:top_k]]
        return CapabilityResult(status=RuntimeStatus.OK, text="capability search results", structured={"hits": hits})


@dataclass
class ToolReadTool:
    runtime: object
    name: str = "tool.read"
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
            return CapabilityResult(status=RuntimeStatus.INVALID, text="name missing", structured={"reason": "name_missing"})
        capability = self.runtime.get_capability_spec(name)
        if capability is None:
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text="capability not found",
                structured={"reason": "capability_not_found"},
            )
        return CapabilityResult(status=RuntimeStatus.OK, text="capability definition", structured={"capability": capability})
