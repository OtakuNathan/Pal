from __future__ import annotations
from pal.shared.tool_protocol import ToolCallIR, new_tool_call
from typing import Any
from pal.foundation import EventEnvelope
from pal.bunshin.scoped_execution import _effective_capability_name
from pal.bunshin.prompt_adapter import bunshin_role
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
) -> list[dict[str, Any]]:
    _ = allowed_capabilities
    build = getattr(execution_runtime, "build_llm_tool_contracts", None)
    if not callable(build):
        raise TypeError("Bunshin execution runtime must expose immutable generation tool contracts")
    # The scoped registry already enforces this role's authority. Output length
    # is not evidence that investigation is complete or a mutation is appropriate.
    return list(build())


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


def bunshin_output_length_recovery_note(pack: BunshinInvocationPack) -> str:
    role = bunshin_role(pack)
    role_guidance = {
        "architect": "Continue the bound architecture declarations and contract; do not implement product behavior.",
        "implementation": "Continue the owned implementation or its focused validation; preserve the accepted contract.",
        "reviewer": "Continue the architecture review: inspect missing evidence or record findings; do not implement or repair the candidate.",
        "verifier": "Continue independent verification: gather missing evidence or record findings; do not repair the candidate.",
    }.get(role, "Continue only the bound role's permitted work.")
    return (
        "The previous assistant response reached the output limit and was discarded. "
        "Do not repeat that response as prose. Resume from the existing workspace and checklist "
        "with one bounded action. Keep reasoning concise and emit complete tool calls when needed. "
        "Read missing decisive evidence when necessary; reuse evidence already established. "
        + role_guidance + " "
        "Do not mutate files or submit merely to recover from truncation. Submit only after the "
        "role's evidence, checklist, and acceptance prerequisites are satisfied. Keep the final reply short."
    )


_BUNSHIN_TOOL_RESULT_RETENTION_CALLS = 5
