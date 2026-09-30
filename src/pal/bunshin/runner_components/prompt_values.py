from __future__ import annotations
from pal.shared.tool_protocol import ToolCallIR, new_tool_call
from typing import Any, Mapping
from pal.execution.tool_facade import EffectKind as ToolEffectKind
from pal.foundation import EventEnvelope
from pal.bunshin.scoped_execution import _effective_capability_name
from pal.shared import PromptAssemblyContext, ToolExecutionResult, BunshinInvocationPack, default_tool_result_text


def _bunshin_prompt_context(
    pack: BunshinInvocationPack,
    *,
    run_id: str,
    event: EventEnvelope | None = None,
    metadata: dict[str, Any],
) -> PromptAssemblyContext:
    logical_scope_id = f"bunshin:{str(run_id or pack.invocation_id).strip()}"
    return PromptAssemblyContext(
        event=event,
        core_mode="bunshin",
        turn_kind="bunshin",
        work_order_id=pack.invocation_id,
        metadata={
            **dict(metadata),
            "artifact_scope_key": logical_scope_id,
            "prompt_cache_scope_id": logical_scope_id,
        },
    )


def _llm_tools_for_allowed(
    execution_runtime: Any,
    allowed_capabilities: list[str],
    *,
    action_only: bool = False,
) -> list[dict[str, Any]]:
    _ = allowed_capabilities
    build = getattr(execution_runtime, "build_llm_tool_contracts", None)
    if not callable(build):
        raise TypeError("Bunshin execution runtime must expose immutable generation tool contracts")
    tools = list(build())
    if not action_only:
        return tools
    generation = getattr(execution_runtime, "registry_generation", None)
    direct_aliases = getattr(generation, "direct_aliases", None)
    indirect_aliases = getattr(generation, "indirect_aliases", None)
    if not isinstance(direct_aliases, Mapping) or not isinstance(indirect_aliases, Mapping):
        raise RuntimeError(
            "output-length recovery requires immutable tool execution semantics"
        )
    read_effects = {
        ToolEffectKind.NONE,
        ToolEffectKind.LOCAL_READ,
        ToolEffectKind.EXTERNAL_READ,
    }
    action_aliases = {
        str(alias)
        for alias, record in direct_aliases.items()
        if getattr(getattr(record, "execution", None), "effect_kind", None)
        not in read_effects
    }
    if any(
        getattr(getattr(record, "execution", None), "effect_kind", None)
        not in read_effects
        for record in indirect_aliases.values()
    ):
        # The indirect record remains hidden from the provider tool list. Its
        # single direct dispatcher is nevertheless an action-capable recovery
        # route for this immutable generation.
        action_aliases.add("call_tool")
    selected = [
        item
        for item in tools
        if str(dict(item.get("function") or {}).get("name") or "").strip()
        in action_aliases
    ]
    if not selected:
        raise RuntimeError(
            "output-length recovery has no action capability in the immutable tool generation"
        )
    return selected


def _provider_call_with_effective_args(
    provider_call: ToolCallIR,
    effective_call: ToolCallIR,
) -> ToolCallIR:
    """Apply Manager defaults without leaking canonical paths back to the facade."""

    args = dict(effective_call.args or {})
    if effective_call.name == "op_tool_call":
        provider_args = dict(provider_call.args or {})
        provider_target = str(provider_args.get("name") or "").strip()
        if provider_target:
            args["name"] = provider_target
    return new_tool_call(
        name=provider_call.name,
        args=args,
        call_id=provider_call.call_id,
    )


def _tool_result_text(result: ToolExecutionResult) -> str:
    return default_tool_result_text(result, fallback_ok="tool completed", fallback_error="tool failed")


def _is_truncation_finish_reason(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"length", "max_tokens", "max_output_tokens", "token_limit", "output_truncated"}


def _tool_call_summary(tool_call: ToolCallIR) -> dict[str, str]:
    return {
        "tool_name": str(tool_call.name or ""),
        "target_name": _effective_capability_name(tool_call),
        "call_id": str(tool_call.call_id or ""),
    }


DEFAULT_BUNSHIN_OUTPUT_LENGTH_RECOVERY_ROUNDS = 3


BUNSHIN_OUTPUT_LENGTH_RECOVERY_NOTE = (
    "The previous assistant response reached the output limit and was discarded; "
    "do not repeat, recap, investigate further, or continue that response as prose. "
    "Resume from the existing workspace and checklist and act now: update the "
    "checklist if necessary, write the smallest compiling/valid scaffold, then fill "
    "it as the next bounded action. Emit complete tool calls, including at least one "
    "action tool call in this round. "
    "Keep the final reply short."
)


_BUNSHIN_TOOL_RESULT_RETENTION_CALLS = 5
