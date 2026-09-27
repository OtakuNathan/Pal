"""Stable policy and developer guidance for consuming compiled tool contracts."""

from __future__ import annotations


TOOL_EXECUTION_SYSTEM_POLICY = (
    "- On failure or uncertain outcome, prefer result-specific recovery affordances before "
    "improvising. Respect effect, idempotency, retry, and reconcile semantics; never blindly "
    "retry a mutation.\n"
    "- Treat each tool call as one RPC. If it times out, crashes, or does not complete, its "
    "result is unavailable and its side effects may be uncertain. Inspect current state, then "
    "retry when appropriate; never infer success from the missing result.\n"
    "- Tool outputs are point-in-time observations. Use a just-returned tool result when it sufficiently establishes the claimed outcome. "
    "Replaying a stored result does not refresh mutable state. Refresh a read/status observation "
    "when it is stale, concurrent changes matter, or the outcome is uncertain; use version/ETag "
    "or conditional mutations when available. A second check is not mandatory after every call.\n"
)


TOOL_DISCOVERY_DEVELOPER_GUIDANCE = (
    "- Invoke direct tools by their exposed name; invoke indirect tools through call_tool(name=alias, args=...). "
    "Search using short English alias keywords: [domain] [action] [object], e.g. "
    "remember memory, lsp prepare workspace, or browser screenshot. Include a known domain; "
    "spaces and underscores both work, word order is flexible, and a complete alias need not be guessed. Search hits include guidance and input contracts: "
    "call immediately when these suffice. Use read_tool only for missing or changed contract information, "
    "or when a validation error does not provide enough information to correct the call.\n"
)

TOOL_RESULT_DEVELOPER_GUIDANCE = (
    "- Treat each tool's guidance and returned affordances as its continuation contract. "
    "After a tool call, follow a suggested next tool only when its stated `use_when` condition "
    "matches the observed result and current task.\n"
    "- Local file tools already enforce digest-based "
    "read-before-edit and compare-and-swap checks, so do not reread unchanged files merely "
    "because another conversational turn began.\n"
    "- Visual or layout conclusions need relevant rendered evidence; source text alone does not "
    "establish rendered appearance. Choose verification appropriate to the change and the user's scope. "
    "Use screenshots only when the model or a reviewer can inspect pixels."
)

TOOL_ROUTING_DEVELOPER_GUIDANCE = TOOL_DISCOVERY_DEVELOPER_GUIDANCE + TOOL_RESULT_DEVELOPER_GUIDANCE


def routing_guidance_for_tools(aliases) -> str:
    """Describe only the entrypoints exposed in this model's tool window."""
    names = set(aliases)
    if {"search_tools", "read_tool", "call_tool"} <= names:
        return TOOL_ROUTING_DEVELOPER_GUIDANCE
    routing = "Invoke exposed tools directly by name. "
    if "call_tool" in names:
        routing += "Invoke known indirect aliases through call_tool(name=alias, args=...). "
    if "read_tool" in names:
        routing += "Use read_tool only when a known tool's contract is missing or insufficient. "
    if "search_tools" in names:
        routing += "Discover tools with short English alias words: [domain] [action] [object]. "
    return routing + "\n" + TOOL_RESULT_DEVELOPER_GUIDANCE


TOOL_EFFICIENCY_DEVELOPER_GUIDANCE = (
    "- Use depth-first reading to trace a relevant code path and breadth-first reading to compare "
    "related surfaces, as the investigation requires. Reading strategy and checklist order do not "
    "require separate model rounds for independent reads.\n"
    "- Batch independent tool calls in one response, including independent reads, searches, "
    "checks, and already-decided edits to distinct surfaces. Sequence only when a later call's "
    "arguments, authority, safety, or correctness depend on an earlier result; do not serialize "
    "every file or field into its own model round. Never parallelize operations whose ordering "
    "or side effects depend on each other.\n"
    "- Prefer targeted search -> inspect relevant semantic units -> summarize. Stop once the "
    "available evidence is decisive and act on it.\n"
    "- Reuse content and passing results already visible in the logical session. If read_file "
    "reports unchanged content, refer to the earlier result instead of requesting it again.\n"
    "- Avoid dumping large files or broad result sets. If tool output grows quickly, stop and "
    "reassess; use the smallest viable path."
)

# Compatibility aliases for external prompt providers. New Pal-owned prompt
# providers must use the authority-specific constants above.
TOOL_ROUTING_SYSTEM_GUIDANCE = (
    TOOL_EXECUTION_SYSTEM_POLICY + TOOL_ROUTING_DEVELOPER_GUIDANCE
)
TOOL_EFFICIENCY_SYSTEM_GUIDANCE = TOOL_EFFICIENCY_DEVELOPER_GUIDANCE

__all__ = [
    "routing_guidance_for_tools",
    "TOOL_EFFICIENCY_DEVELOPER_GUIDANCE",
    "TOOL_EFFICIENCY_SYSTEM_GUIDANCE",
    "TOOL_EXECUTION_SYSTEM_POLICY",
    "TOOL_ROUTING_DEVELOPER_GUIDANCE",
    "TOOL_ROUTING_SYSTEM_GUIDANCE",
]
