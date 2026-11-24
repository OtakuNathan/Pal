"""Stable policy and developer guidance for consuming compiled tool contracts."""

from __future__ import annotations


TOOL_DISCOVERY_STOP_RULE = (
    "- Stop discovery once a reasonable query, domain adjustment, or directory check yields no suitable tool; "
    "report possible unavailability and uncertainty instead of looping or substituting an unrelated capability.\n"
)

TOOL_DISCOVERY_STOP_HINT = (
    "If rephrasing, corrected filters and the relevant directory still yield no suitable tool, "
    "report possible unavailability and uncertainty; stop repeating discovery."
)


TOOL_EXECUTION_SYSTEM_POLICY = (
    "Plugins provide most extensible capabilities, including channel providers and some memory backends. "
    "For development, updates or repairs owned by a known plugin, use pal.plugin.development. "
    "Most plugins support hot reload. Verify the affected capability after activation.\n"
    "For Pal configuration, resident code, shared contracts or unclear ownership, use pal.self.maintenance. "
    "Use supported tools or the official `pal` / `pal memory` CLI with an explicit `--runtime-root`.\n"
    "Tool execution and error recovery:\n"
    "- Treat each tool call as one RPC. On timeout, crash, or incomplete output, side effects may be uncertain—never "
    "infer success from a missing result and never blindly retry a mutation. Prefer result-specific recovery affordances.\n"
    "- Tool outputs describe state when the call ran; Replaying a stored result does not refresh mutable state. "
    "Refresh observations when stale or uncertain, and inspect the returned snapshot file when error output is truncated.\n"
    "- Tool result metadata:\n"
    "  - kind: rejected (refused before start), failed (check effect), complete (still inspect item failures).\n"
    "  - effect: effect=none | effect=not_started | effect=not_applied (no intended change made; fix and retry); "
    "effect=applied (change persisted, possibly partially—do not repeat completed items); "
    "effect=unknown (harness cannot confirm state—inspect before repeating a mutation).\n"
    "  - retry: retry=correct_input (fix arguments/precondition); retry=safe (retry permitted); "
    "retry=reconcile_first (inspect state first); retry=do_not_retry (do not repeat).\n"
    "  - error_code / recovery / affordances: category, result-specific guidance, and suggested next calls within "
    "the authorized task (not actions already performed or new authorization).\n"
)


TOOL_DISCOVERY_DEVELOPER_GUIDANCE = (
    "- Invoke tools with a native schema directly by name; invoke indirect aliases via call_tool(name=alias, args=...). "
    "Historical names do not guarantee current availability—use the live tool surface and discovered contracts.\n"
    "- Capabilities grow dynamically via plugins. For unfamiliar indirect tools, call search_tools with a short "
    "affirmative English task description in query (keep negations and extra constraints for contract review) and a "
    "known owner domain; omit namespace by default and omit domain when unsure. Pass domain alone to browse it, "
    "or call search_tools() to list domains.\n"
    "- Search hits and recovery candidates include complete contracts—verify against the full request (including unmatched_terms) and invoke "
    "directly. Use read_tool only for directory entries or missing/changed contracts; inspect truncated outputs with "
    "read_file on the returned snapshot.\n"
    + TOOL_DISCOVERY_STOP_RULE
)

TOOL_RESULT_DEVELOPER_GUIDANCE = (
    "- Use each tool's guidance and returned affordances to decide the next action. "
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
        routing += "Discover tools with a known owner domain and a task description. " + TOOL_DISCOVERY_STOP_RULE
    return routing + "\n" + TOOL_RESULT_DEVELOPER_GUIDANCE


TOOL_EFFICIENCY_DEVELOPER_GUIDANCE = (
    "- Batch independent tool calls in one response, including independent reads, searches, "
    "checks, and already-decided edits to distinct surfaces. Sequence only when a later call's "
    "arguments, authority, safety, or correctness depend on an earlier result; do not serialize "
    "every file or field into its own model round. Never parallelize operations whose ordering "
    "or side effects depend on each other.\n"
    "- For code investigation, use run_shell with rg -n to locate relevant definitions, then read_file "
    "with ranges around the hits. Stop searching once the evidence supports the next action.\n"
    "- If read_file reports unchanged content, reuse the earlier result in this conversation. "
    "Reuse passing checks while the relevant code and environment remain unchanged.\n"
    "- Use an alternative such as run_shell only when it actually satisfies the original task and is authorized; "
    "a request for a dedicated tool must not be replaced with an unrelated operation."
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
    "TOOL_DISCOVERY_STOP_RULE",
    "TOOL_EFFICIENCY_SYSTEM_GUIDANCE",
    "TOOL_EXECUTION_SYSTEM_POLICY",
    "TOOL_ROUTING_DEVELOPER_GUIDANCE",
    "TOOL_ROUTING_SYSTEM_GUIDANCE",
]
