from __future__ import annotations

from pal.shared import MinionInvocationPack


_V2_RUNNER_METADATA_KEYS = frozenset(
    {
        "allow_text_only_completion",
        "agent_session",
        "clarification_answers",
        "control_route",
        "debug_log",
        "heartbeat_interval_seconds",
        "initial_skill_injections",
        "llm_round_timeout_seconds",
        "manager_turn_timeout_seconds",
        "max_output_tokens",
        "temperature",
        "max_tool_rounds",
        "minion_debug_log_enabled",
        "minion_v2",
        "preferred_endpoint_id",
        "preferred_endpoint_source",
        "prompt_log_enabled",
        "requirements_brief",
        "skill_manual_context",
        "timeout_seconds",
    }
)


def sanitize_runner_session_pack(pack: MinionInvocationPack) -> MinionInvocationPack:
    """Remove manager-only data before one V2 role invocation enters its sandbox."""
    metadata = {
        key: value
        for key, value in dict(pack.metadata or {}).items()
        if key in _V2_RUNNER_METADATA_KEYS
    }
    return MinionInvocationPack.from_dict({**pack.to_dict(), "metadata": metadata})
