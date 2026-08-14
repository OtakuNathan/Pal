"""Stable system guidance for consuming compiled tool contracts."""

from __future__ import annotations


TOOL_ROUTING_SYSTEM_GUIDANCE = (
    "- Treat each tool's guidance and returned affordances as its continuation contract. "
    "After a tool call, follow a suggested next tool only when its stated `use_when` condition "
    "matches the observed result and current task.\n"
    "- On failure or uncertain outcome, prefer result-specific recovery affordances, then the "
    "tool description's `Failure next steps`, before improvising. Respect effect, idempotency, "
    "retry, and reconcile semantics; never blindly retry a mutation.\n"
    "- Treat each tool call as one RPC. If it times out, crashes, or does not complete, its "
    "result is unavailable and its side effects may be uncertain. Inspect current state, then "
    "retry when appropriate; never infer success from the missing result.\n"
    "- Tool outputs are point-in-time observations. Replaying a stored result does not refresh "
    "mutable external or runtime state. Before asserting current state or making an external "
    "mutation based on it, rerun the original read/status/list/reconcile tool or use a "
    "version, ETag, or conditional mutation. Local file tools already enforce digest-based "
    "read-before-edit and compare-and-swap checks, so do not reread unchanged files merely "
    "because another conversational turn began.\n"
    "- For UI, CSS, or layout work, normalized page text/HTML and source inspection are not "
    "rendered-layout verification. Before diagnosing and after changing layout, inspect "
    "representative selectors with computed styles, bounding geometry, and actual element "
    "gaps using an available rendered-layout inspection capability. Cover ordinary, nested, "
    "and edge-case content. Use screenshots only when the active model or a reviewer can "
    "inspect pixels."
)


TOOL_EFFICIENCY_SYSTEM_GUIDANCE = (
    "- Batch independent tool calls in one response, including independent reads, searches, "
    "checks, and already-decided edits to distinct surfaces. Sequence only when a later call's "
    "arguments, authority, safety, or correctness depend on an earlier result; do not serialize "
    "every file or field into its own model round.\n"
    "- Prefer targeted search -> inspect relevant semantic units -> summarize. Stop once the "
    "available evidence is decisive and act on it.\n"
    "- Reuse content and passing results already visible in the logical session. If read_file "
    "reports unchanged content, refer to the earlier result instead of requesting it again.\n"
    "- Avoid dumping large files or broad result sets. If tool output grows quickly, stop and "
    "reassess; use the smallest viable path."
)


__all__ = ["TOOL_EFFICIENCY_SYSTEM_GUIDANCE", "TOOL_ROUTING_SYSTEM_GUIDANCE"]
