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
    "because another conversational turn began."
)


__all__ = ["TOOL_ROUTING_SYSTEM_GUIDANCE"]
