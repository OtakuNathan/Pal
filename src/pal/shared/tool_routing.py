"""Stable policy and developer guidance for consuming compiled tool contracts."""

from __future__ import annotations


TOOL_EXECUTION_SYSTEM_POLICY = (
    "- On failure or uncertain outcome, prefer result-specific recovery affordances, then the "
    "tool description's `Failure next steps`, before improvising. Respect effect, idempotency, "
    "retry, and reconcile semantics; never blindly retry a mutation.\n"
    "- Treat each tool call as one RPC. If it times out, crashes, or does not complete, its "
    "result is unavailable and its side effects may be uncertain. Inspect current state, then "
    "retry when appropriate; never infer success from the missing result.\n"
    "- Tool outputs are point-in-time observations. Use a just-returned tool result when it sufficiently establishes the claimed outcome. "
    "Replaying a stored result does not refresh mutable state. Refresh a read/status observation "
    "when it is stale, concurrent changes matter, or the outcome is uncertain; use version/ETag "
    "or conditional mutations when available. A second check is not mandatory after every call.\n"
)


TOOL_ROUTING_DEVELOPER_GUIDANCE = (
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
    "TOOL_EFFICIENCY_DEVELOPER_GUIDANCE",
    "TOOL_EFFICIENCY_SYSTEM_GUIDANCE",
    "TOOL_EXECUTION_SYSTEM_POLICY",
    "TOOL_ROUTING_DEVELOPER_GUIDANCE",
    "TOOL_ROUTING_SYSTEM_GUIDANCE",
]
