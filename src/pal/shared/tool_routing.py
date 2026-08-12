"""Stable system guidance for consuming compiled tool contracts."""

from __future__ import annotations


TOOL_ROUTING_SYSTEM_GUIDANCE = (
    "- Treat each tool's guidance and returned affordances as its continuation contract. "
    "After a tool call, follow a suggested next tool only when its stated `use_when` condition "
    "matches the observed result and current task.\n"
    "- On failure or uncertain outcome, prefer result-specific recovery affordances, then the "
    "tool description's `Failure next steps`, before improvising. Respect effect, idempotency, "
    "retry, and reconcile semantics; never blindly retry a mutation."
)


__all__ = ["TOOL_ROUTING_SYSTEM_GUIDANCE"]
