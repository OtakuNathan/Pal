"""Stable policy and developer guidance for consuming compiled tool contracts."""

from __future__ import annotations


TOOL_EXECUTION_SYSTEM_POLICY = (
    "- On failure or uncertain outcome, prefer result-specific recovery affordances before "
    "improvising. Respect effect, idempotency, retry, and reconcile semantics; never blindly "
    "retry a mutation.\n"
    "- Treat each tool call as one RPC. If it times out, crashes, or does not complete, its "
    "result is unavailable and its side effects may be uncertain. Inspect current state, then "
    "retry when appropriate; never infer success from the missing result.\n"
    "- Tool outputs describe state when the call ran. "
    "Replaying a stored result does not refresh mutable state. Refresh a read/status observation "
    "when it is stale, concurrent changes matter, or the outcome is uncertain; use version/ETag "
    "or conditional mutations when available. A second check is not mandatory after every call.\n"
    "Tool result metadata maps to actions:\n"
    "- kind=rejected means the requested action was refused before it started; kind=failed means the tool "
    "could not report success, so check effect to learn whether changes occurred; "
    "kind=complete means the handler returned, but check any item failures and recovery text.\n"
    "- effect describes changes, independently of success or failure: effect=none means no side effect; "
    "effect=not_started means the requested action did not start; effect=not_applied means the intended "
    "change was not made. On failure, fix the reported problem before retrying.\n"
    "- effect=applied means the action was applied, possibly only for some requested items; a later error "
    "does not undo applied work. Do not repeat completed items. effect=unknown means the harness cannot "
    "confirm what changed; inspect current state before repeating a mutation.\n"
    "- retry=correct_input means change arguments or satisfy the stated precondition; retry=safe permits "
    "a retry when useful, without promising success; retry=reconcile_first means inspect state first; "
    "retry=do_not_retry means do not repeat the call.\n"
    "- error_code identifies the failure category; the error text explains what happened. recovery gives "
    "result-specific guidance; affordances suggest next tool calls, not actions already performed or "
    "new authorization. Use them only within the authorized task.\n"
    "- Read the actual cause and any per-item failures, even when kind=complete. If output is shortened, "
    "use the returned snapshot to read the full error; an incomplete preview is not complete evidence. "
    "An output-delivery failure does not prove the original action failed or had no effect.\n"
)


TOOL_DISCOVERY_DEVELOPER_GUIDANCE = (
    "- Invoke direct tools by their exposed name; invoke indirect tools through call_tool(name=alias, args=...). "
    "Use search_tools with short English alias keywords: [domain] [action] [object], e.g. "
    "remember memory, lsp prepare workspace, or browser screenshot. Include a known domain; "
    "spaces and underscores both work, word order is flexible, and a complete alias need not be guessed. Search hits include guidance and input contracts: "
    "call immediately when these suffice. Use read_tool only for missing or changed contract information, "
    "or when a validation error does not provide enough information to correct the call.\n"
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
        routing += "Discover tools with short English alias words: [domain] [action] [object]. "
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
    "- If search_tools finds no useful capability, try one rephrased query. Then use a known suitable tool "
    "such as run_shell when available, or report the capability gap."
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
